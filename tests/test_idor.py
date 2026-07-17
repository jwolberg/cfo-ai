"""The IDOR suite. `architecture.md` §7.1's gate on the authz pattern.

> An IDOR here exposes someone's complete financial life; one forgotten `WHERE` clause is not an
> acceptable single point of failure.

There are two layers — the repository's `WHERE household_id = :h`, and Postgres RLS — and the
whole value of having two is that they fail independently. So each is proven **with the other
removed**:

- `TestTheRepositoryScopesWithoutRls` runs as a **superuser**, where RLS does not apply at all.
  Anything that passes there passed because the repository's own SQL scoped it.
- `TestRlsScopesWithoutTheRepository` (in `test_schema.py`) runs raw SQL as `cfo_app` with no
  repository involved.
- `TestBothTogether` is the real configuration.

Defense in depth that is only ever tested end-to-end is one layer wearing a disguise: if the
repository silently stopped scoping, an end-to-end test would still pass on RLS alone, and nobody
would learn until the day RLS was misconfigured.

**Read this before trusting the suite.** The identity feeding it is not real — `backend/auth.py`
is a single shared API key and ticket 0024 keeps it, so *any* caller may select *any* household.
The mechanism is real and tested; the identity is not. That gap is deliberate (synthetic households
have no owner to authenticate as, and Clerk lands with Plaid) and is flagged in `USERS.md` rather
than only here. An IDOR suite is reassuring in a way a shared key does not earn.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest
from sqlalchemy import text

from backend.db.models import HOUSEHOLD_SCOPED, RLS_VAR
from backend.db.repository import Repository, repository
from backend.db.session import (
    RlsWouldNotBind,
    assert_rls_binds,
    current_household,
    household_scope,
)
from tests.conftest import requires_db

pytestmark = requires_db

ALICE, BOB = "alice", "bob"


@pytest.fixture
def two_households(db):
    """Alice and Bob, each with an account, a card, a policy, a decision, and a snapshot.

    Arranged as superuser so the arranging itself is not the thing under test.
    """
    with db.begin():
        for h in (ALICE, BOB):
            db.execute(text("INSERT INTO households (id, archetype) VALUES (:h, 'test')"), {"h": h})
            db.execute(
                text(
                    "INSERT INTO accounts (id, household_id, kind, balance, connection,"
                    " balance_age_days) VALUES (:a, :h, 'checking', '100.00', 'healthy', 0)"
                ),
                {"a": f"acct-{h}", "h": h},
            )
            db.execute(
                text(
                    "INSERT INTO cards (id, household_id, apr, close_day_of_month, grace_days,"
                    " statement_balance, statement_due_date, minimum_payment, unbilled_balance,"
                    " next_close_date, behavior) VALUES (:c, :h, '0.2399', 20, 21, '1000.00',"
                    " '2026-02-10', '25.00', '0.00', '2026-01-20', 'revolver')"
                ),
                {"c": f"card-{h}", "h": h},
            )
            db.execute(
                text(
                    "INSERT INTO policies (household_id, buffer_floor, max_sweep,"
                    " max_weekly_sweep) VALUES (:h, '750.00', '500.00', '1000.00')"
                ),
                {"h": h},
            )
            db.execute(
                text(
                    "INSERT INTO decisions (id, household_id, day, action, amount, reasons,"
                    " engine_version) VALUES (:d, :h, '2026-03-02', 'refuse', '0.00', '[]', 'v1')"
                ),
                {"d": f"dec-{h}", "h": h},
            )
            db.execute(
                text(
                    "INSERT INTO snapshots (id, household_id, day, payload)"
                    " VALUES (:s, :h, '2026-03-02', '{}')"
                ),
                {"s": f"snap-{h}", "h": h},
            )
            # A linked Plaid Item per household (ticket 0034). Without a row here, `plaid_items`
            # would ride the HOUSEHOLD_SCOPED parametrization with an *empty* table — structural
            # coverage that never proves alice's item is invisible to bob. RLS on a new scoped
            # table is exactly the mechanism this repo has shipped built-tested-never-exercised.
            db.execute(
                text(
                    "INSERT INTO plaid_items (id, household_id, plaid_item_id, access_token)"
                    " VALUES (:id, :h, :pid, 'access-sandbox-x')"
                ),
                {"id": f"pi-{h}", "h": h, "pid": f"item-{h}"},
            )
    return db


class TestTheRepositoryScopesWithoutRls:
    """**RLS is not helping here.** These run as the superuser that owns the tables, so every
    policy is bypassed outright. Anything that passes below passed because the repository's own
    SQL scoped it — which is what makes this a test of the repository rather than of Postgres.
    """

    def test_reads_return_only_the_scoped_household(self, two_households) -> None:
        repo = Repository(conn=two_households, household_id=ALICE)

        assert [a["id"] for a in repo.accounts()] == [f"acct-{ALICE}"]
        assert [c["id"] for c in repo.cards()] == [f"card-{ALICE}"]
        assert [d["id"] for d in repo.decisions()] == [f"dec-{ALICE}"]
        assert repo.policy() is not None

    def test_a_repository_cannot_be_repointed_after_construction(self, two_households) -> None:
        """Frozen on purpose: a repository handed to a function cannot be quietly rescoped."""
        repo = Repository(conn=two_households, household_id=ALICE)
        # FrozenInstanceError specifically, not a blind Exception: an AttributeError from a typo
        # in the field name would satisfy a looser assertion while proving nothing about frozenness.
        with pytest.raises(dataclasses.FrozenInstanceError):
            repo.household_id = BOB  # type: ignore[misc]
        assert repo.household_id == ALICE

    def test_a_scoped_read_of_another_households_day_finds_nothing(self, two_households) -> None:
        import datetime as dt

        repo = Repository(conn=two_households, household_id=ALICE)
        with two_households.begin():
            two_households.execute(
                text(
                    "INSERT INTO decisions (id, household_id, day, action, amount, reasons,"
                    " engine_version) VALUES ('bob-only', :h, '2026-04-01', 'sweep', '10.00',"
                    " '[]', 'v1')"
                ),
                {"h": BOB},
            )
        assert repo.decision_on(dt.date(2026, 4, 1)) is None


class TestBothTogether:
    """The real configuration: the repository scoped, RLS bound to the same household.

    Runs on `app_engine` — a **non-superuser**. That is not a detail. The first draft of this class
    used `db_engine`, which is the superuser that owns the tables, and a superuser bypasses RLS
    outright even when FORCEd: every test below passed while proving nothing about RLS at all.
    `test_the_app_engine_is_not_secretly_a_superuser` is what keeps that from coming back.
    """

    def test_the_app_engine_is_not_secretly_a_superuser(self, app_engine) -> None:
        """If this fails, every other test in this class is vacuous — they would pass on a
        connection RLS does not apply to, and the suite would report a security property it never
        checked. This is the assertion that makes the rest of the class mean anything."""
        with app_engine.connect() as conn:
            row = conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            ).one()
        assert row.rolsuper is False, "the app engine is superuser — RLS does not apply to it"
        assert row.rolbypassrls is False, "the app engine has BYPASSRLS — RLS does not apply"

    @pytest.mark.parametrize("table", HOUSEHOLD_SCOPED)
    def test_no_scoped_table_leaks_another_household(
        self, two_households, app_engine, table
    ) -> None:
        """Every scoped table, not a sample. Adding a table without a policy fails here."""
        with repository(app_engine, ALICE) as repo:
            rows = (
                repo.conn.execute(
                    text(f"SELECT household_id FROM {table}")  # noqa: S608 — table from a fixed tuple
                )
                .scalars()
                .all()
            )
        assert set(rows) <= {ALICE}, f"{table} leaked another household: {set(rows)}"

    def test_a_write_lands_under_the_scoped_household(self, two_households, app_engine) -> None:
        with repository(app_engine, ALICE) as repo:
            repo.add_account(
                account_id="new-acct",
                kind="savings",
                balance=Decimal("2400.00"),
                connection="healthy",
                balance_age_days=0,
            )
        with two_households.begin():
            got = two_households.execute(
                text("SELECT household_id FROM accounts WHERE id='new-acct'")
            ).scalar()
        assert got == ALICE

    def test_a_repository_cannot_write_into_another_household(
        self, two_households, app_engine
    ) -> None:
        """WITH CHECK. Reading someone else's row and creating one under their name are the same
        breach in opposite directions; the second is the one people forget.

        The repository binds `household_id` from its own scope, so this is RLS catching an attempt
        the repository could not express — which is exactly the backstop it exists to be.
        """
        with (
            pytest.raises(Exception, match="row-level security"),
            repository(app_engine, ALICE) as repo,
        ):
            repo.conn.execute(
                text(
                    "INSERT INTO accounts (id, household_id, kind, balance, connection,"
                    " balance_age_days) VALUES ('smuggled', :h, 'checking', '1.00', 'healthy', 0)"
                ),
                {"h": BOB},
            )


class TestTheScopeDoesNotSurviveTheConnection:
    """⚠️ The trap this whole suite exists around.

    Neon pools connections. A plain `SET app.household_id` persists for the life of the
    *connection*, not the transaction, so the next checkout inherits it — an IDOR wearing a
    security feature's clothes. **A suite that opens one connection and reuses it never sees the
    difference**, which is why these tests are about the pool rather than about a query.
    """

    def test_the_variable_is_unset_on_a_fresh_checkout(self, app_engine) -> None:
        with repository(app_engine, ALICE):
            pass  # scope opened and closed

        # A new checkout — very possibly the same physical connection, returned to the pool.
        with app_engine.connect() as conn:
            assert current_household(conn) is None, (
                "the household scope survived the transaction and is now on a pooled connection: "
                "the next request to reuse it inherits alice's scope"
            )

    def test_the_variable_does_not_outlive_its_transaction_on_one_connection(
        self, app_engine
    ) -> None:
        """The same claim, without the pool in the way: `set_config(..., true)` is transaction-
        local, so the value is gone the moment the transaction ends even if the connection lives.
        """
        with app_engine.connect() as conn:
            with household_scope(conn, ALICE):
                assert current_household(conn) == ALICE
            assert current_household(conn) is None

    def test_a_second_scope_on_a_reused_connection_does_not_see_the_first(
        self, two_households, app_engine
    ) -> None:
        """The consequence, end to end: alice's request then bob's, on a pool of one."""
        with repository(app_engine, ALICE) as repo:
            assert [a["id"] for a in repo.accounts()] == [f"acct-{ALICE}"]
        with repository(app_engine, BOB) as repo:
            assert [a["id"] for a in repo.accounts()] == [f"acct-{BOB}"]

    def test_the_scope_is_bound_not_interpolated(self, app_engine) -> None:
        """`SET LOCAL` cannot take a bind parameter, so the obvious spelling forces a
        request-supplied value into the SQL string. `set_config(:var, :hid, true)` is a function
        and takes it as a parameter — the safe spelling and the correct one are the same.

        The proof is behavioural: a household id containing SQL is stored and returned verbatim
        rather than executed or truncated.
        """
        hostile = "alice'; DROP TABLE households; --"
        with app_engine.connect() as conn, household_scope(conn, hostile):
            assert current_household(conn) == hostile

        with app_engine.connect() as conn:
            still_there = conn.execute(
                text("SELECT count(*) FROM information_schema.tables WHERE table_name='households'")
            ).scalar()
        assert still_there == 1, "households was dropped — the scope was interpolated, not bound"


def test_an_unscoped_connection_reads_nothing(two_households, db_engine, as_app) -> None:
    """RLS fails closed, which is `architecture.md` [1.1]'s second principle at the storage layer.

    A query that forgot to scope returns zero rows rather than everything. That is the difference
    between a bug and a breach: `current_setting(RLS_VAR, true)` is NULL when unset, and NULL never
    equals a household_id.
    """
    for table in HOUSEHOLD_SCOPED:
        seen = as_app(
            None,
            lambda c, t=table: c.execute(text(f"SELECT * FROM {t}")).all(),  # noqa: S608
        )
        assert seen == [], f"{table} returned rows to an unscoped session"


def test_rls_var_is_the_only_name_anything_uses() -> None:
    """One constant, one name. A policy keyed on `app.household_id` and a session that sets
    `app.household` is an outage at best and a silent bypass at worst — `current_setting` of an
    unset variable is NULL, and NULL never matches, so the failure is *empty reads*, not an error.
    """
    assert RLS_VAR == "app.household_id"


class TestTheConnectingRoleMustNotBypassRls:
    """The check that turns a schema guarantee into a deployment one.

    Every policy in this suite is worth exactly what the *connecting role* makes it worth, and that
    is a deploy-time fact CI cannot see. `architecture.md` [4]'s two layers collapse to one — with
    no error, no symptom, and a green test suite — the moment the app connects as a role holding
    SUPERUSER or BYPASSRLS.

    **This is not hypothetical. Neon's default role fails it.** Measured 2026-07-16: `neondb_owner`
    is provisioned with `rolbypassrls = true`; connected as that role and scoped to one household,
    a `SELECT` returned both. Pasting the connection string Neon hands you into `DATABASE_URL` —
    the obvious deployment — would have shipped exactly that.
    """

    def test_a_bypassrls_role_is_refused_at_startup(self, db_engine) -> None:
        """The superuser fixture is a stand-in for Neon's neondb_owner: same property, same
        consequence, and the only one available in CI."""
        with (
            db_engine.connect() as conn,
            pytest.raises(RlsWouldNotBind, match="BYPASSRLS|SUPERUSER"),
        ):
            assert_rls_binds(conn)

    def test_the_app_role_is_accepted(self, app_engine) -> None:
        with app_engine.connect() as conn:
            assert_rls_binds(conn)  # does not raise

    def test_the_error_names_the_role_and_the_consequence(self, db_engine) -> None:
        """A startup failure that says "permission problem" gets worked around with a superuser.
        This one has to say what breaks, or the fix will be to grant more."""
        with db_engine.connect() as conn, pytest.raises(RlsWouldNotBind) as e:
            assert_rls_binds(conn)
        msg = str(e.value)
        assert "row-level security" in msg
        assert "every request" in msg or "readable by every" in msg
        assert "neondb_owner" in msg, "the message must name the role that actually does this"

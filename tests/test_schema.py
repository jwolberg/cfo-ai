"""The schema Alembic produces — RLS, partitioning, and exact money.

Ticket 0020. These run against a real Postgres and nothing else: RLS and declarative partitioning
are the subject, so a SQLite stand-in would test a different artifact and report green.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import text

from backend.db.models import HOUSEHOLD_SCOPED
from backend.db.session import (
    PlaidAccessTokenWouldLeak,
    assert_plaid_tokens_safe_at_rest,
    plaid_env,
)
from tests.conftest import requires_db

pytestmark = requires_db


def _household(conn, hid: str) -> None:
    conn.execute(text("INSERT INTO households (id, archetype) VALUES (:i, 'test')"), {"i": hid})


def _account(conn, hid: str, aid: str, balance: str = "100.00") -> None:
    conn.execute(
        text(
            "INSERT INTO accounts (id, household_id, kind, balance, connection, balance_age_days)"
            " VALUES (:a, :h, 'checking', :b, 'healthy', 0)"
        ),
        {"a": aid, "h": hid, "b": Decimal(balance)},
    )


class TestMoneyIsExact:
    """ADR-0002 [2.2]: "a cent that round-trips through a float is no longer the cent the engine
    decided on." `NUMERIC` is exact natively — the one place the database is *stronger* than the
    artifact it replaces, and why the `$dec` codec now survives only for the JSONB payload."""

    @pytest.mark.parametrize(
        "amount",
        [
            "0.10",  # the canonical float casualty: 0.1 has no binary representation
            "0.07",
            "1234567.89",
            "999999999.99",  # near the NUMERIC(14,2) ceiling
            "0.00",
        ],
    )
    def test_a_decimal_survives_the_round_trip_exactly(self, db, amount: str) -> None:
        with db.begin():
            _household(db, "h1")
            _account(db, "h1", "a1", amount)
            got = db.execute(text("SELECT balance FROM accounts WHERE id='a1'")).scalar()

        assert got == Decimal(amount)
        assert isinstance(got, Decimal), f"got {type(got).__name__} — a float reached the money"
        assert str(got) == amount, "the exact digits, not merely an equal value"

    def test_every_money_column_is_numeric_not_float(self, db) -> None:
        """The guard against someone adding a `float` column later. `double precision` here would
        pass every value test above and still be wrong."""
        rows = db.execute(
            text(
                "SELECT table_name, column_name, data_type FROM information_schema.columns"
                " WHERE table_schema='public' AND data_type IN ('double precision','real')"
            )
        ).all()
        assert rows == [], f"floating-point columns in the schema: {rows}"

    def test_apr_holds_its_exact_rate(self, db) -> None:
        """23.99% must come back as 0.23990, not 0.2398999999. `interest.py` computes the number
        the company is graded on from this."""
        with db.begin():
            _household(db, "h1")
            db.execute(
                text(
                    "INSERT INTO cards (id, household_id, apr, close_day_of_month, grace_days,"
                    " statement_balance, statement_due_date, minimum_payment, unbilled_balance,"
                    " next_close_date, behavior) VALUES ('c1','h1',:apr,20,21,'14000.00',"
                    " '2026-02-10','280.00','0.00','2026-01-20','revolver')"
                ),
                {"apr": Decimal("0.2399")},
            )
            got = db.execute(text("SELECT apr FROM cards WHERE id='c1'")).scalar()
        assert got == Decimal("0.2399")


class TestAprMayBeUnknown:
    """`decision-engine.md` §6.3: Plaid does not report APR for many issuers, and the engine
    refuses to rank rather than guess. The column has to allow the absence it reasons about."""

    def test_a_card_with_no_apr_is_storable(self, db) -> None:
        with db.begin():
            _household(db, "h1")
            db.execute(
                text(
                    "INSERT INTO cards (id, household_id, apr, close_day_of_month, grace_days,"
                    " statement_balance, statement_due_date, minimum_payment, unbilled_balance,"
                    " next_close_date, behavior) VALUES ('c1','h1',NULL,20,21,'900.00',"
                    " '2026-02-10','25.00','0.00','2026-01-20','unknown')"
                )
            )
            assert db.execute(text("SELECT apr FROM cards WHERE id='c1'")).scalar() is None

    def test_an_impossible_apr_is_rejected(self, db) -> None:
        """`engine/models.py` validates 0 <= apr <= 2. The same claim, where the data lives."""
        with pytest.raises(Exception, match="ck_cards_apr_range"), db.begin():
            _household(db, "h1")
            db.execute(
                text(
                    "INSERT INTO cards (id, household_id, apr, close_day_of_month, grace_days,"
                    " statement_balance, statement_due_date, minimum_payment,"
                    " unbilled_balance, next_close_date, behavior) VALUES"
                    " ('c1','h1','3.5',20,21,'900.00','2026-02-10','25.00','0.00',"
                    " '2026-01-20','revolver')"
                )
            )


class TestTheDecisionLogIsPartitioned:
    def test_rows_route_across_a_partition_boundary(self, db) -> None:
        """Two days either side of a month boundary land in different partitions and both read
        back. Partitioning is invisible to the caller or it is a bug."""
        with db.begin():
            _household(db, "h1")
            for i, day in enumerate(("2026-01-31", "2026-02-01")):
                db.execute(
                    text(
                        "INSERT INTO decisions (id, household_id, day, action, amount, reasons,"
                        " engine_version) VALUES (:i,'h1',:d,'refuse','0.00','[]','test')"
                    ),
                    {"i": f"d{i}", "d": day},
                )

            assert db.execute(text("SELECT count(*) FROM decisions")).scalar() == 2
            where = (
                db.execute(text("SELECT tableoid::regclass::text FROM decisions ORDER BY day"))
                .scalars()
                .all()
            )

        assert where == ["decisions_2026_01", "decisions_2026_02"], (
            "rows did not route to the month partitions"
        )

    def test_a_day_outside_every_range_lands_in_default_rather_than_vanishing(self, db) -> None:
        """The DEFAULT partition is not a convenience. Without it this INSERT raises; with it the
        row is kept and findable. Either is acceptable — silently dropping it is not."""
        with db.begin():
            _household(db, "h1")
            db.execute(
                text(
                    "INSERT INTO decisions (id, household_id, day, action, amount, reasons,"
                    " engine_version) VALUES ('d1','h1','2019-06-01','refuse','0.00','[]','test')"
                )
            )
            where = db.execute(
                text("SELECT tableoid::regclass::text FROM decisions WHERE id='d1'")
            ).scalar()
        assert where == "decisions_default"

    def test_a_sweep_of_zero_is_rejected(self, db) -> None:
        """`engine/decide.py`'s MIN_SWEEP and the artifact's own validator both say a SWEEP moves
        money and a REFUSE moves none. The schema says it too, so no path can write otherwise."""
        with pytest.raises(Exception, match="ck_decisions_amount_matches_action"), db.begin():
            _household(db, "h1")
            db.execute(
                text(
                    "INSERT INTO decisions (id, household_id, day, action, amount, reasons,"
                    " engine_version) VALUES ('d1','h1','2026-01-05','sweep','0.00','[]','t')"
                )
            )


class TestRowLevelSecurity:
    """**Proven as `cfo_app`, never as the superuser that arranges the data.**

    A superuser bypasses RLS outright — even FORCEd — so the obvious version of every test below
    passes while proving nothing. `conftest.as_app` becomes the application role inside the
    transaction, which is what makes the policy bind.
    """

    def test_rls_is_enabled_and_forced_on_every_scoped_table(self, db) -> None:
        """FORCE matters: without it the table owner is exempt, and the migration's own role would
        sail straight through the policy it just created."""
        rows = dict(
            db.execute(
                text(
                    "SELECT relname, (relrowsecurity AND relforcerowsecurity)"
                    " FROM pg_class WHERE relname = ANY(:t)"
                ),
                {"t": list(HOUSEHOLD_SCOPED)},
            ).all()
        )
        for table in HOUSEHOLD_SCOPED:
            assert rows.get(table) is True, f"{table}: RLS not enabled AND forced"

    def test_a_scoped_session_cannot_see_another_household(self, db, as_app) -> None:
        with db.begin():
            _household(db, "alice")
            _household(db, "bob")
            _account(db, "alice", "a-alice", "111.00")
            _account(db, "bob", "a-bob", "222.00")

        seen = as_app("alice", lambda c: c.execute(text("SELECT id FROM accounts")).scalars().all())
        assert seen == ["a-alice"], "alice's session saw bob's account"

    def test_a_scoped_session_cannot_write_into_another_household(self, db, as_app) -> None:
        """WITH CHECK, not just USING. Reading someone else's row and *creating* one under their
        name are the same breach in opposite directions, and the second is the one people forget.
        """
        with db.begin():
            _household(db, "alice")
            _household(db, "bob")

        with pytest.raises(Exception, match="row-level security"):
            as_app("alice", lambda c: _account(c, "bob", "smuggled", "999.00"))

    def test_a_scoped_session_cannot_update_or_delete_another_households_row(
        self, db, as_app
    ) -> None:
        with db.begin():
            _household(db, "alice")
            _household(db, "bob")
            _account(db, "bob", "a-bob", "222.00")

        updated = as_app(
            "alice",
            lambda c: (
                c.execute(text("UPDATE accounts SET balance='0.00' WHERE id='a-bob'")).rowcount
            ),
        )
        deleted = as_app(
            "alice", lambda c: c.execute(text("DELETE FROM accounts WHERE id='a-bob'")).rowcount
        )

        assert updated == 0, "alice updated bob's row"
        assert deleted == 0, "alice deleted bob's row"
        with db.begin():
            still = db.execute(text("SELECT balance FROM accounts WHERE id='a-bob'")).scalar()
        assert still == Decimal("222.00")

    def test_an_unscoped_session_sees_nothing(self, db, as_app) -> None:
        """RLS fails closed — `architecture.md` [1.1]'s second principle, at the storage layer.

        `current_setting(RLS_VAR, true)` returns NULL when unset, and NULL never equals a
        household_id. So a query that forgot to scope returns zero rows rather than everything,
        which is the difference between a bug and a breach.
        """
        with db.begin():
            _household(db, "alice")
            _account(db, "alice", "a-alice", "111.00")

        seen = as_app(None, lambda c: c.execute(text("SELECT id FROM accounts")).scalars().all())
        assert seen == []

    @pytest.mark.parametrize("table", HOUSEHOLD_SCOPED)
    def test_every_scoped_table_has_an_isolation_policy(self, db, table: str) -> None:
        """Per-table, so adding a table without a policy fails here rather than in production."""
        got = db.execute(
            text("SELECT count(*) FROM pg_policies WHERE tablename=:t AND policyname=:p"),
            {"t": table, "p": f"{table}_household_isolation"},
        ).scalar()
        assert got == 1, f"{table} has no isolation policy"


class TestTheAppRoleIsNotPrivileged:
    def test_the_app_role_is_not_superuser_and_does_not_bypass_rls(self, db) -> None:
        """Either flag makes every policy in this file decoration."""
        row = db.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname='cfo_app'")
        ).one()
        assert row.rolsuper is False, "cfo_app is superuser — RLS does not apply to it"
        assert row.rolbypassrls is False, "cfo_app has BYPASSRLS — RLS does not apply to it"

    def test_the_app_role_cannot_touch_alembic_version(self, db) -> None:
        """Migration bookkeeping is not the application's business — and granting it is what made
        `downgrade()` fail, since a role holding a grant cannot be dropped."""
        got = db.execute(
            text(
                "SELECT count(*) FROM information_schema.role_table_grants"
                " WHERE grantee='cfo_app' AND table_name='alembic_version'"
            )
        ).scalar()
        assert got == 0


class TestTheTenantIsTheHousehold:
    def test_no_table_carries_a_user_id(self, db) -> None:
        """`architecture.md` [4] scoped everything by `user_id` until 2026-07-16. A `user` is a
        login; the household is the tenant, and one household may have two logins — for this
        product that is not a footnote, since a spouse's spending is what breaks a forecast."""
        rows = (
            db.execute(
                text(
                    "SELECT table_name FROM information_schema.columns"
                    " WHERE table_schema='public' AND column_name='user_id'"
                )
            )
            .scalars()
            .all()
        )
        assert rows == [], f"tables scoped by user_id rather than household_id: {rows}"

    def test_plaid_tables_do_not_exist_yet(self, db) -> None:
        """ADR-0004 [2]: named in `architecture.md` [4], shape not yet known. Empty tables invite
        guessed columns; the migration is cheap once Plaid makes the shape real."""
        rows = (
            db.execute(
                text(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema='public' AND table_name IN"
                    " ('items','transactions','recurring_events','payments','users')"
                )
            )
            .scalars()
            .all()
        )
        assert rows == [], f"speculative tables exist: {rows}"


class TestPlaidItems:
    """The Plaid transport rung's first table (ticket 0034).

    RLS is covered for free: `plaid_items` is in `HOUSEHOLD_SCOPED`, so the parametrized policy and
    FORCE tests above and `tests/test_idor.py`'s leak test already exercise it. These are the
    column-level claims that parametrization does not make.
    """

    def _item(self, conn, hid: str, row_id: str, item_id: str, token: str = "access-sandbox-x"):
        conn.execute(
            text(
                "INSERT INTO plaid_items (id, household_id, plaid_item_id, access_token)"
                " VALUES (:id, :h, :pid, :tok)"
            ),
            {"id": row_id, "h": hid, "pid": item_id, "tok": token},
        )

    def test_an_item_defaults_to_healthy_with_no_cursor_and_no_sync(self, db) -> None:
        """Ships `healthy`; the cursor and `last_successful_sync_at` are absent, not sentinels —
        "sync from the beginning" is the missing cursor, and freshness is unknown before a sync."""
        with db.begin():
            _household(db, "h1")
            self._item(db, "h1", "pi-1", "item-1")
            row = db.execute(
                text("SELECT status, cursor, last_successful_sync_at FROM plaid_items")
            ).one()
        assert row.status == "healthy"
        assert row.cursor is None
        assert row.last_successful_sync_at is None

    def test_plaid_item_id_is_unique_across_households(self, db) -> None:
        """It is Plaid's key, not ours, and the doorbell resolves `item_id → household` before any
        scope is set (ADR-0005). Two households sharing one would make that lookup ambiguous."""
        with pytest.raises(Exception, match="uq_plaid_items_plaid_item_id"), db.begin():
            _household(db, "h1")
            _household(db, "h2")
            self._item(db, "h1", "pi-1", "shared-item")
            self._item(db, "h2", "pi-2", "shared-item")

    def test_an_unknown_status_is_rejected(self, db) -> None:
        """`status` is engine.models.ConnectionState — healthy | login_required | disconnected —
        and the CHECK says so where the data lives, so no path can write a fourth value."""
        with pytest.raises(Exception, match="ck_plaid_items_status"), db.begin():
            _household(db, "h1")
            db.execute(
                text(
                    "INSERT INTO plaid_items (id, household_id, plaid_item_id, access_token,"
                    " status) VALUES ('pi-x', 'h1', 'item-x', 'tok', 'expired')"
                )
            )


class TestPlaidTokenStartGuard:
    """`assert_plaid_tokens_safe_at_rest` — the tripwire on the day `PLAID_ENV` flips to production
    before KMS makes `access_token` ciphertext. Same shape and same spirit as the `assert_rls_binds`
    tests: the failure is silent (a plaintext credential), so the guard is a startup refusal.
    """

    def test_sandbox_boots(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PLAID_ENV", "sandbox")
        assert_plaid_tokens_safe_at_rest(db)  # does not raise

    def test_unset_defaults_to_sandbox_and_boots(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """The safe default: no Plaid configured means nothing to protect."""
        monkeypatch.delenv("PLAID_ENV", raising=False)
        assert plaid_env() == "sandbox"
        assert_plaid_tokens_safe_at_rest(db)  # does not raise

    def test_production_is_refused_while_encryption_is_not_active(
        self, db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PLAID_ENV", "production")
        with pytest.raises(PlaidAccessTokenWouldLeak, match="plaintext"):
            assert_plaid_tokens_safe_at_rest(db)

    def test_a_non_null_dek_id_does_not_satisfy_the_guard(
        self, db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The false "looks protected". A `dek_id` with no key behind it — every synthetic
        household today — must not let production boot. The guard keys on the encryption
        *capability*, never on `dek_id` presence, or a stray placeholder would satisfy a check
        while the token stayed clear.
        """
        monkeypatch.setenv("PLAID_ENV", "production")
        with db.begin():
            db.execute(
                text(
                    "INSERT INTO households (id, archetype, dek_id)"
                    " VALUES ('h1', 'test', 'dek-123')"
                )
            )
        with pytest.raises(PlaidAccessTokenWouldLeak):
            assert_plaid_tokens_safe_at_rest(db)


class TestPlaidTransactions:
    """The append-only landing table (ticket 0036). Every sync outcome is an INSERT; corrections are
    new rows. RLS rides the `HOUSEHOLD_SCOPED` parametrization and the IDOR suite — these are the
    append-only and Plaid-shape claims those do not make."""

    def _txn(
        self,
        conn,
        hid: str,
        row_id: str,
        txn_id: str = "ptx-1",
        change_type: str = "added",
        amount: str | None = "12.34",
    ) -> None:
        conn.execute(
            text(
                "INSERT INTO plaid_transactions (id, household_id, plaid_item_id,"
                " plaid_account_id, plaid_transaction_id, amount, date, name, change_type)"
                " VALUES (:id, :h, 'item-1', 'acct-1', :ptx, :amt, '2026-03-01', 'Coffee', :ct)"
            ),
            {
                "id": row_id,
                "h": hid,
                "ptx": txn_id,
                "amt": Decimal(amount) if amount is not None else None,
                "ct": change_type,
            },
        )

    def _removed(self, conn, hid: str, row_id: str, txn_id: str = "ptx-1") -> None:
        """A Plaid-shaped `removed`: only the two ids, no amount/date/name/merchant_name."""
        conn.execute(
            text(
                "INSERT INTO plaid_transactions (id, household_id, plaid_item_id,"
                " plaid_account_id, plaid_transaction_id, change_type)"
                " VALUES (:id, :h, 'item-1', 'acct-1', :ptx, 'removed')"
            ),
            {"id": row_id, "h": hid, "ptx": txn_id},
        )

    def test_modified_and_removed_are_new_rows_not_in_place_edits(self, db) -> None:
        """The same `plaid_transaction_id` appears as added, then modified, then removed — three
        rows, nothing updated or deleted in place. A UNIQUE on that column would reject the very
        corrections append-only exists to keep."""
        with db.begin():
            _household(db, "h1")
            self._txn(db, "h1", "r1", txn_id="ptx-9", change_type="added", amount="10.00")
            self._txn(db, "h1", "r2", txn_id="ptx-9", change_type="modified", amount="12.00")
            self._removed(db, "h1", "r3", txn_id="ptx-9")
            rows = db.execute(
                text(
                    "SELECT change_type, amount FROM plaid_transactions"
                    " WHERE plaid_transaction_id='ptx-9' ORDER BY id"
                )
            ).all()
        assert [r.change_type for r in rows] == ["added", "modified", "removed"]
        assert [r.amount for r in rows] == [Decimal("10.00"), Decimal("12.00"), None]

    def test_a_plaid_shaped_removed_payload_inserts_with_nulls(self, db) -> None:
        """`removed` carries only `transaction_id`/`account_id`. The value columns are NULL — the
        honest absence, not a fabricated full row."""
        with db.begin():
            _household(db, "h1")
            self._removed(db, "h1", "r1")
            row = db.execute(
                text(
                    "SELECT amount, date, name, merchant_name FROM plaid_transactions WHERE id='r1'"
                )
            ).one()
        assert (row.amount, row.date, row.name, row.merchant_name) == (None, None, None, None)

    def test_a_numeric_amount_round_trips_a_decimal_exactly(self, db) -> None:
        """NUMERIC, never float (ADR-0002 [2.2]) — the same rule the money columns hold."""
        with db.begin():
            _household(db, "h1")
            self._txn(db, "h1", "r1", amount="1234.56")
            got = db.execute(text("SELECT amount FROM plaid_transactions WHERE id='r1'")).scalar()
        assert got == Decimal("1234.56")
        assert isinstance(got, Decimal), f"got {type(got).__name__} — a float reached the money"

    def test_an_unknown_change_type_is_rejected(self, db) -> None:
        with pytest.raises(Exception, match="ck_plaid_transactions_change_type"), db.begin():
            _household(db, "h1")
            db.execute(
                text(
                    "INSERT INTO plaid_transactions (id, household_id, plaid_item_id,"
                    " plaid_account_id, plaid_transaction_id, change_type)"
                    " VALUES ('r1', 'h1', 'item-1', 'acct-1', 'ptx-1', 'deleted')"
                )
            )

    def test_the_app_role_cannot_update_or_delete_an_append_only_row(self, db, as_app) -> None:
        """Append-only is a **grant**, not a convention: `cfo_app` holds SELECT and INSERT and
        nothing else, so no application path can rewrite or erase history even by mistake. Proven by
        trying both as the app role and being refused at the privilege layer."""
        with db.begin():
            _household(db, "h1")
            self._txn(db, "h1", "r1")

        with pytest.raises(Exception, match="permission denied"):
            as_app(
                "h1",
                lambda c: c.execute(text("UPDATE plaid_transactions SET name='x' WHERE id='r1'")),
            )
        with pytest.raises(Exception, match="permission denied"):
            as_app(
                "h1",
                lambda c: c.execute(text("DELETE FROM plaid_transactions WHERE id='r1'")),
            )

        with db.begin():
            still = db.execute(text("SELECT name FROM plaid_transactions WHERE id='r1'")).scalar()
        assert still == "Coffee", "the row was rewritten despite the grant"


class TestTransfers:
    """The append-only money-movement ledger (ticket 0039, the sweep-execution rung). Every state
    transition is an INSERT; corrections and supersessions are new rows. RLS rides the
    `HOUSEHOLD_SCOPED` parametrization and the IDOR suite — these are the append-only, CHECK, and
    money-shape claims those do not make."""

    def _transfer(
        self,
        conn,
        hid: str,
        row_id: str,
        *,
        leg: str = "debit",
        state: str = "submitted",
        direction: str = "debit",
        amount: str = "50.00",
        provider: str = "increase",
        decision_id: str = "dec-1",
        decision_date: str = "2026-03-02",
        provider_transfer_id: str | None = None,
    ) -> None:
        conn.execute(
            text(
                "INSERT INTO transfers (id, household_id, target_card_id, decision_id,"
                " decision_date, leg, state, direction, amount, provider, provider_transfer_id,"
                " idempotency_key) VALUES (:id, :h, 'card-1', :dec, :dd, :leg, :state, :dir, :amt,"
                " :prov, :ptid, :idem)"
            ),
            {
                "id": row_id,
                "h": hid,
                "dec": decision_id,
                "dd": decision_date,
                "leg": leg,
                "state": state,
                "dir": direction,
                "amt": Decimal(amount),
                "prov": provider,
                "ptid": provider_transfer_id,
                "idem": f"{hid}-{decision_date}-{leg}-submit",
            },
        )

    def test_a_state_transition_is_a_new_row_not_an_in_place_edit(self, db) -> None:
        """One leg advancing submitted → settled is two rows sharing a `provider_transfer_id`, not
        an UPDATE. A UNIQUE there would reject the transition append-only exists to record."""
        with db.begin():
            _household(db, "h1")
            self._transfer(db, "h1", "t1", state="submitted", provider_transfer_id="inc-9")
            self._transfer(db, "h1", "t2", state="settled", provider_transfer_id="inc-9")
            rows = (
                db.execute(
                    text(
                        "SELECT state FROM transfers WHERE provider_transfer_id='inc-9'"
                        " ORDER BY created_at, id"
                    )
                )
                .scalars()
                .all()
            )
        assert rows == ["submitted", "settled"]

    def test_a_superseding_transfer_shares_the_slot_without_a_unique_rejection(self, db) -> None:
        """A same-day re-decision mints a new `decision_id` against the same `(household,
        decision_date)` slot. Both rows insert — the at-most-one-submitted invariant is the saga's
        slot lock (U4), not a UNIQUE that would reject this legitimate supersession (KTD-2)."""
        with db.begin():
            _household(db, "h1")
            self._transfer(db, "h1", "t1", decision_id="dec-1")
            self._transfer(db, "h1", "t2", decision_id="dec-2")  # same slot, new decision
            n = db.execute(
                text("SELECT count(*) FROM transfers WHERE decision_date='2026-03-02'")
            ).scalar()
        assert n == 2

    def test_a_numeric_amount_round_trips_a_decimal_exactly(self, db) -> None:
        """NUMERIC, never float (ADR-0002 [2.2]) — the row is a debit, so the cent must be exact."""
        with db.begin():
            _household(db, "h1")
            self._transfer(db, "h1", "t1", amount="1234.56")
            got = db.execute(text("SELECT amount FROM transfers WHERE id='t1'")).scalar()
        assert got == Decimal("1234.56")
        assert isinstance(got, Decimal), f"got {type(got).__name__} — a float reached the money"

    @pytest.mark.parametrize(
        ("column", "value", "constraint"),
        [
            ("state", "posted", "ck_transfers_state"),  # a vendor's word, not our ledger's
            ("leg", "reserve", "ck_transfers_leg"),
            ("direction", "sideways", "ck_transfers_direction"),
            ("provider", "dwolla", "ck_transfers_provider"),  # not the chosen debit provider
        ],
    )
    def test_an_out_of_enum_value_is_rejected(self, db, column, value, constraint) -> None:
        """Each enumerated column states its valid set where the data lives; nothing writes past."""
        with pytest.raises(Exception, match=constraint), db.begin():
            _household(db, "h1")
            self._transfer(db, "h1", "t1", **{column: value})

    def test_a_nonpositive_amount_is_rejected(self, db) -> None:
        """A transfer moves money; zero or negative is a bug the schema refuses."""
        with pytest.raises(Exception, match="ck_transfers_amount_positive"), db.begin():
            _household(db, "h1")
            self._transfer(db, "h1", "t1", amount="0.00")

    def test_the_app_role_cannot_update_or_delete_an_append_only_row(self, db, as_app) -> None:
        """Append-only is a **grant**, not a convention: `cfo_app` holds SELECT and INSERT and
        nothing else, so no application path rewrites a debit's history even by mistake. Proven by
        trying both as the app role and being refused at the privilege layer — the highest-stakes
        instance of this guarantee in the schema, because the row is money that already moved."""
        with db.begin():
            _household(db, "h1")
            self._transfer(db, "h1", "t1", state="submitted")

        with pytest.raises(Exception, match="permission denied"):
            as_app(
                "h1",
                lambda c: c.execute(text("UPDATE transfers SET state='settled' WHERE id='t1'")),
            )
        with pytest.raises(Exception, match="permission denied"):
            as_app("h1", lambda c: c.execute(text("DELETE FROM transfers WHERE id='t1'")))

        with db.begin():
            still = db.execute(text("SELECT state FROM transfers WHERE id='t1'")).scalar()
        assert still == "submitted", "the row was rewritten despite the grant"


def test_deleted_at_records_the_shred_not_a_soft_delete(db) -> None:
    """ADR-0004 C4. `deleted_at` is set when the household's KMS key is destroyed — the bytes are
    genuinely unrecoverable at that point. It is not a filter the repository has to remember, and
    the rows deliberately survive so the append-only audit trail holds and decision *counts* still
    aggregate for `prd.md` §5.2's population-wide guardrail.

    There is no key behind `dek_id` yet: synthetic households have no PII, and the KMS wiring lands
    with Plaid. The column is the expensive half to retrofit, which is why it is here now.
    """
    with db.begin():
        db.execute(
            text(
                "INSERT INTO households (id, archetype, dek_id, deleted_at)"
                " VALUES ('gone','test','dek-123',:t)"
            ),
            {"t": dt.datetime(2026, 7, 16, tzinfo=dt.timezone.utc)},
        )
        row = db.execute(text("SELECT dek_id, deleted_at FROM households WHERE id='gone'")).one()
    assert row.dek_id == "dek-123"
    assert row.deleted_at is not None

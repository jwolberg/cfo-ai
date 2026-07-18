"""The identity schema — `users`, `household_members`, and the `households_for_user` lookup.

Ticket 0046 (the identity rung's U1), ADR-0008. Runs against a real Postgres like the rest of the
schema suite — RLS, the SECURITY DEFINER function, and the grants are the subject, and a stand-in
would test a different artifact and report green.

The load-bearing claims here are the two the plan calls the recurring failure of this repo:

- `users` is deliberately **outside** the forced set and reached **only by single key** — a leak of
  PII from a table with no RLS backstop is the whole risk of a platform table, so the discipline
  that replaces RLS is asserted rather than assumed.
- the `household_members` leak test is **non-vacuous** — household B reads *zero* of A's real
  memberships, not zero rows from an empty table.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from backend.db import repository as repo_mod
from backend.db.models import HOUSEHOLD_SCOPED, PLATFORM_TABLES
from backend.db.repository import (
    Repository,
    add_user,
    get_user_by_id,
    get_user_by_stytch_id,
    households_for_user,
    repository,
)
from tests.conftest import requires_db

pytestmark = requires_db

ALICE, BOB = "alice", "bob"


def _household(conn, hid: str) -> None:
    conn.execute(text("INSERT INTO households (id, archetype) VALUES (:i, 'test')"), {"i": hid})


def _member(conn, hid: str, uid: str, role: str = "owner") -> None:
    conn.execute(
        text("INSERT INTO users (id, stytch_user_id, email) VALUES (:u, :s, :e)"),
        {"u": uid, "s": f"stytch-{uid}", "e": f"{uid}@example.test"},
    )
    conn.execute(
        text("INSERT INTO household_members (household_id, user_id, role) VALUES (:h, :u, :r)"),
        {"h": hid, "u": uid, "r": role},
    )


class TestUsersIsPlatformNotHouseholdScoped:
    """A user predates every household (KTD-1), so `users` is platform-level: no `household_id`, no
    RLS, and outside the forced set. The exclusion is an asserted decision, not an oversight."""

    def test_users_is_not_in_the_household_scoped_set(self) -> None:
        assert "users" not in HOUSEHOLD_SCOPED
        assert "users" in PLATFORM_TABLES

    def test_the_two_sets_are_disjoint(self) -> None:
        """A table cannot be both forced-and-scoped and deliberately-unscoped. Catches a rename or
        a copy-paste that lands a table in both lists."""
        assert set(HOUSEHOLD_SCOPED).isdisjoint(PLATFORM_TABLES)

    def test_users_has_no_row_level_security(self, db) -> None:
        """The other half of the claim, at the database rather than in a Python tuple: `users`
        carries neither ENABLE nor a policy. If a later migration forced it, this fails — which is
        the prompt to reconsider the single-key access discipline, not to silently proceed."""
        forced = db.execute(
            text("SELECT relrowsecurity FROM pg_class WHERE relname = 'users'")
        ).scalar()
        assert forced is False, "users has RLS enabled — it is meant to be platform-level"
        policies = db.execute(
            text("SELECT count(*) FROM pg_policies WHERE tablename = 'users'")
        ).scalar()
        assert policies == 0

    def test_users_carries_no_household_id_column(self, db) -> None:
        got = db.execute(
            text(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_schema='public' AND table_name='users' AND column_name='household_id'"
            )
        ).scalar()
        assert got == 0


class TestUsersIsReachedOnlyBySingleKey:
    """PII with no RLS backstop, so the discipline that replaces RLS: every read is by a unique key,
    returning at most one row. An unfiltered scan is exactly the cross-user leak a future admin
    route could add by accident (KTD-1's PII guard)."""

    def test_the_module_exposes_no_user_list_function(self) -> None:
        """There is no `all_users` / `list_users` / `users` free function, and `Repository` has no
        method that returns every user. The only sanctioned reads are the two single-key lookups."""
        for banned in ("all_users", "list_users", "users", "every_user"):
            assert not hasattr(repo_mod, banned), (
                f"repository.{banned} exists — an unfiltered users read is the PII leak KTD-1 bars"
            )
        assert not hasattr(Repository, "users"), "Repository.users would be an unscoped users read"

    def test_a_single_key_lookup_returns_at_most_the_keyed_row(self, db) -> None:
        """Two users exist; each lookup returns exactly the one it keyed on — never both. The
        behavioural proof that the read path cannot spill a second user's PII."""
        with db.begin():
            add_user(db, user_id="u-alice", stytch_user_id="s-alice", email="alice@example.test")
            add_user(db, user_id="u-bob", stytch_user_id="s-bob", email="bob@example.test")

        by_stytch = get_user_by_stytch_id(db, "s-alice")
        by_id = get_user_by_id(db, "u-bob")
        assert by_stytch is not None and by_stytch["id"] == "u-alice"
        assert by_id is not None and by_id["stytch_user_id"] == "s-bob"
        assert get_user_by_stytch_id(db, "s-nobody") is None

    def test_provisioning_is_idempotent_on_stytch_id(self, db) -> None:
        """JIT provisioning under the concurrent first-login race: a second insert of the same
        `stytch_user_id` is a no-op, not a unique violation, and the first row wins."""
        with db.begin():
            add_user(db, user_id="u-1", stytch_user_id="s-dup", email="first@example.test")
            add_user(db, user_id="u-2", stytch_user_id="s-dup", email="second@example.test")
        got = get_user_by_stytch_id(db, "s-dup")
        assert got is not None and got["id"] == "u-1", "the second provisioning overwrote the first"
        n = db.execute(text("SELECT count(*) FROM users WHERE stytch_user_id = 's-dup'")).scalar()
        assert n == 1


class TestHouseholdMembersIsScopedAndConstrained:
    def test_an_unknown_role_is_rejected(self, db) -> None:
        with pytest.raises(Exception, match="ck_household_members_role"), db.begin():
            _household(db, "h1")
            db.execute(text("INSERT INTO users (id, stytch_user_id) VALUES ('u1', 's1')"))
            db.execute(
                text(
                    "INSERT INTO household_members (household_id, user_id, role)"
                    " VALUES ('h1', 'u1', 'admin')"
                )
            )

    def test_a_scoped_session_reads_zero_of_another_households_memberships(self, db, app_engine):
        """**Non-vacuous.** Alice and Bob each have a real membership row; scoped to Alice, the app
        role sees only hers. Proven as `cfo_app` (RLS binds), against real rows, not an empty table.
        """
        with db.begin():
            _household(db, ALICE)
            _household(db, BOB)
            _member(db, ALICE, "u-alice", "owner")
            _member(db, BOB, "u-bob", "viewer")

        with repository(app_engine, ALICE) as repo:
            seen = repo.conn.execute(text("SELECT user_id FROM household_members")).scalars().all()
        assert seen == ["u-alice"], f"alice's session saw another household's membership: {seen}"

    def test_a_scoped_session_cannot_write_a_membership_into_another_household(
        self, db, app_engine
    ):
        """WITH CHECK, not just USING: scoped to Alice, the app role cannot INSERT a membership
        under Bob even naming him correctly. `add_membership` binds the household from its scope, so
        this is RLS catching an attempt the repository could not express."""
        with db.begin():
            _household(db, ALICE)
            _household(db, BOB)
            db.execute(text("INSERT INTO users (id, stytch_user_id) VALUES ('u-x', 's-x')"))

        with (
            pytest.raises(Exception, match="row-level security"),
            repository(app_engine, ALICE) as repo,
        ):
            repo.conn.execute(
                text(
                    "INSERT INTO household_members (household_id, user_id, role)"
                    " VALUES (:h, 'u-x', 'owner')"
                ),
                {"h": BOB},
            )


class TestHouseholdsForUserResolvesAcrossTheForcedTable:
    """ADR-0008's whole reason for existing. `household_members` is FORCE'd, so an unscoped app
    session sees nothing in it — but `authorize_household` must read it *before* a scope is set to
    know which household to bind. The SECURITY DEFINER `households_for_user` is the narrow, audited
    alternative to granting the app role BYPASSRLS, mirroring `plaid_household_for_item` (ADR-0005).
    """

    def test_a_direct_read_of_household_members_without_a_scope_sees_nothing(self, db, app_engine):
        """The FORCE'd table, as `authorize_household`'s own role would see it before scoping."""
        with db.begin():
            _household(db, ALICE)
            _member(db, ALICE, "u-alice", "owner")
        with app_engine.connect() as conn:
            seen = conn.execute(text("SELECT count(*) FROM household_members")).scalar()
        assert seen == 0, (
            "an unscoped app session read household_members directly — RLS did not bind"
        )

    def test_the_definer_function_resolves_exactly_a_users_households(self, db, app_engine):
        """Same unscoped session, through the sanctioned function: it returns exactly the households
        the user belongs to, and nothing of anyone else's."""
        with db.begin():
            _household(db, ALICE)
            _household(db, BOB)
            db.execute(text("INSERT INTO users (id, stytch_user_id) VALUES ('u-multi', 's-multi')"))
            db.execute(
                text(
                    "INSERT INTO household_members (household_id, user_id, role)"
                    " VALUES (:a, 'u-multi', 'owner'), (:b, 'u-multi', 'viewer')"
                ),
                {"a": ALICE, "b": BOB},
            )
            _member(db, ALICE, "u-alice", "owner")  # someone else's membership, must not appear

        with app_engine.connect() as conn:
            resolved = households_for_user(conn, "u-multi")
        assert set(resolved) == {ALICE, BOB}

    def test_a_user_with_no_memberships_resolves_to_the_empty_set(self, db, app_engine):
        with db.begin():
            db.execute(
                text("INSERT INTO users (id, stytch_user_id) VALUES ('u-lonely', 's-lonely')")
            )
        with app_engine.connect() as conn:
            assert households_for_user(conn, "u-lonely") == []


class TestIsDemoFlag:
    def test_is_demo_defaults_false(self, db) -> None:
        """A real household is never part of the demo plane by omission (KTD-10)."""
        with db.begin():
            _household(db, "h-real")
            got = db.execute(text("SELECT is_demo FROM households WHERE id='h-real'")).scalar()
        assert got is False

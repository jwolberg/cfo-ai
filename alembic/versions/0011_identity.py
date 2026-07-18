"""users, household_members, the households_for_user lookup, and households.is_demo.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-18

Ticket 0046, the identity rung's first unit (U1). See
`docs/plans/2026-07-17-003-feat-identity-and-settings-controls-plan.md` and ADR-0008.

This revision builds the precondition every user route has been missing: a real user, and a
membership graph that says which households that user may touch. Three things land, and the reasons
they are shaped the way they are matter more than the DDL:

1. **`users` is a platform table, deliberately NOT household-scoped.** A user exists before any
   household and independent of all of them — the same posture as the FBO funding account and the
   raw webhook store. So it carries no `household_id`, no RLS policy, and is *outside*
   `HOUSEHOLD_SCOPED`. That exclusion is asserted, not incidental (`tests/test_identity_schema.py`,
   `tests/test_schema.py`). It carries PII with no RLS backstop, so the application reaches it only
   by single-key lookup (`backend/db/repository.py`) — never an unfiltered scan.

2. **`household_members` is the bridge, and it IS scoped.** Keyed by `household_id`, RLS enabled AND
   forced exactly like every tenant table (`0001`/`0005`). `role` is CHECK-constrained to
   `owner`/`viewer`: `viewer` is the public demo principal (read-only), `owner` may write. This is
   the one role distinction the rung ships (KTD-10).

3. **`households_for_user(text) RETURNS SETOF text`, SECURITY DEFINER.** The membership lookup has
   the same chicken-and-egg as the webhook doorbell (ADR-0005): to know which household to scope to
   we must read `household_members`, but it is FORCE'd, so an unscoped app session sees nothing.
   Resolved the same way — a narrow definer function that returns only ids, never `BYPASSRLS` on the
   app role. ADR-0005 [3]'s rule ("any new definer function is a new ADR") is why ADR-0008 exists.

`households.is_demo` is added here too (default false, backfilled true for the already-seeded
synthetic households) — the flag KTD-10's demo plane and the link-exchange refusal both key on.

The table names are **literals**, never `from backend.db.models import HOUSEHOLD_SCOPED` — ticket
`0033`'s rule: a migration states what *it* did. `downgrade()` drops the function, the two tables,
and the column, and touches no role and no database it never created (`0001`/`0020` traps).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from backend.db.models import RLS_VAR
from backend.db.session import APP_ROLE

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

# Named literally, not read out of `HOUSEHOLD_SCOPED` — see the module docstring and ticket 0033.
_USERS = "users"
_MEMBERS = "household_members"


def upgrade() -> None:
    # --- households.is_demo ----------------------------------------------------------
    # The demo-plane flag (KTD-10). Default false so a real household is never a demo one by
    # omission. Backfilled true for the synthetic households already seeded (archetype IS NOT NULL is
    # exactly "this row was walked from an archetype", i.e. synthetic) so the public demo keeps
    # working without a re-seed — the same backfill discipline U4's policy_events uses.
    op.add_column(
        "households",
        sa.Column("is_demo", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    op.execute("UPDATE households SET is_demo = true WHERE archetype IS NOT NULL;")

    # --- users (platform, NOT household-scoped, no RLS) ------------------------------
    op.create_table(
        _USERS,
        # Our stable id — the identifier the deferred Plaid Link rung passes to /link/token/create
        # as `client_user_id` (KTD-1). Ours, not Stytch's, so a provider swap does not rekey users.
        sa.Column("id", sa.Text, primary_key=True),
        # Stytch's own id for the user; the edge adapter resolves a verified session to this, and
        # this to our `id`. UNIQUE so a second login for the same Stytch user maps to one row.
        sa.Column("stytch_user_id", sa.Text, nullable=False),
        # Nullable: some auth factors do not carry a verified email, and a NULL is the honest absence
        # rather than a fabricated address. PII — reached only by single-key lookup (no RLS here).
        sa.Column("email", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Set when the user is erased (the crypto-shred half of `households.deleted_at`). Not a
        # per-request filter; recorded for the audit trail that outlives the plaintext.
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_users_stytch_user_id", _USERS, ["stytch_user_id"])

    # SELECT/INSERT/UPDATE — JIT provisioning inserts, a later soft-delete stamps `deleted_at`. No
    # DELETE: a user is shredded, never hard-deleted, so the append-only membership/audit trail
    # holds. **No RLS** — this table is deliberately platform-level (KTD-1); the guard against a
    # cross-user PII leak is the single-key access discipline in the repository, asserted in tests.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON {_USERS} TO {APP_ROLE};")

    # --- household_members (HOUSEHOLD_SCOPED, RLS-forced) ----------------------------
    op.create_table(
        _MEMBERS,
        sa.Column(
            "household_id",
            sa.Text,
            sa.ForeignKey("households.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        # A bridge FK, NOT the tenant key — the household is still the tenant (`0001`'s rule, and
        # `test_no_table_carries_a_user_id` is relaxed to say exactly this). ON DELETE CASCADE so a
        # shredded user's memberships go with them.
        sa.Column(
            "user_id",
            sa.Text,
            sa.ForeignKey(f"{_USERS}.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        # owner writes, viewer reads (KTD-10). The one role distinction the rung ships.
        sa.Column("role", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("role IN ('owner', 'viewer')", name="ck_household_members_role"),
    )
    op.create_index("ix_household_members_user", _MEMBERS, ["user_id"])

    # Membership is mutable state — a member is added, a role changed, access revoked — so the app
    # role holds full DML, unlike the append-only ledgers. RLS still binds every one of them.
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_MEMBERS} TO {APP_ROLE};")

    # Both halves of the policy, FORCEd — the reasoning is `0001`'s and `0005`'s. USING governs what
    # is visible, WITH CHECK what may be written; without the second a session scoped to A could
    # INSERT a membership under B. `current_setting(RLS_VAR, true)` is NULL when unset and NULL never
    # equals a household_id, so an unscoped query sees zero rows: RLS fails closed.
    op.execute(f"ALTER TABLE {_MEMBERS} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {_MEMBERS} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY {_MEMBERS}_household_isolation ON {_MEMBERS}
            USING      (household_id = current_setting('{RLS_VAR}', true))
            WITH CHECK (household_id = current_setting('{RLS_VAR}', true));
        """
    )

    # --- households_for_user: the one sanctioned cross-scope read (ADR-0008) ---------
    # Mirrors `plaid_household_for_item` (0007) exactly: STABLE, SECURITY DEFINER, pinned
    # search_path so the body cannot be hijacked by a caller's path, and it returns nothing but ids.
    # It runs as its owner (the migration-runner, a superuser/BYPASSRLS role in every environment
    # that runs migrations) and so reads the FORCE'd `household_members` the app role cannot — but
    # only within this one narrow, audited surface. Preferred over granting cfo_app BYPASSRLS, which
    # would unscope every query the application makes.
    op.execute(
        f"""
        CREATE FUNCTION households_for_user(p_user_id text)
        RETURNS SETOF text
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT household_id FROM {_MEMBERS} WHERE user_id = p_user_id;
        $$;
        """
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION households_for_user(text) TO {APP_ROLE};")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS households_for_user(text);")
    op.execute(f"DROP POLICY IF EXISTS {_MEMBERS}_household_isolation ON {_MEMBERS};")
    # household_members before users: it FKs users. The grants, index and constraints go with each.
    op.drop_table(_MEMBERS)
    op.drop_table(_USERS)
    op.drop_column("households", "is_demo")

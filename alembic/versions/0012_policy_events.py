"""policy_events — the append-only guardrail history that retires the mutable policies row.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-18

Ticket 0049 (the identity rung's U4), KTD-6. Policy is the guardrail set — safety-critical — and the
codebase's identity is append-only auditability (`decisions` freeze their input, `transfers` are
append-only, state is a projection). So the source of record for policy becomes an append-only
`policy_events` table, and `Repository.policy()` reads the **latest event per household** ordered by
`seq` (a monotonic identity, not `created_at`, since a seed transaction can tie on the clock — the
sweep rung's `transfers.seq` lesson). History cannot be derived from current state, but current state
*can* be derived from history — which is the "don't keep a second copy that can disagree" prior.

Two things this migration must get right:

1. **Append-only is a grant, not a convention.** `cfo_app` is granted SELECT + INSERT and nothing
   else, so no application path can rewrite or erase a guardrail change — the same posture as
   `plaid_transactions` and `transfers`.

2. **The backfill is not optional.** Every already-seeded household (including the deployed reviewer
   surface) has a `policies` row and zero `policy_events`. Without a backfill, `policy()` would read
   nothing after cutover and the household would lose its guardrails. So one `policy_events` row is
   written per existing `policies` row *before* `policies` is dropped.

The `loosened` flag records a change that *weakened* a guardrail (KTD-9) — the audit marker the
eventual step-up retrofit and any review find them by. The table names are literals (ticket 0033).
`downgrade()` recreates `policies`, backfills it from the latest event, and drops `policy_events`.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from backend.db.models import RLS_VAR
from backend.db.session import APP_ROLE

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_TABLE = "policy_events"
_OLD = "policies"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Text, primary_key=True),
        # Monotonic insertion order. `created_at` is `now()` (transaction-start), so a seed
        # transaction writing several rows ties on it; `seq` is the total order `policy()` reads.
        sa.Column("seq", sa.BigInteger, sa.Identity(always=True), nullable=False),
        sa.Column(
            "household_id",
            sa.Text,
            sa.ForeignKey("households.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Who made the change. NULL for a seeded/initial event (no user), and ON DELETE SET NULL so a
        # shredded user's audit rows survive with the actor nulled rather than the history erased.
        sa.Column(
            "changed_by",
            sa.Text,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("buffer_floor", sa.Numeric(14, 2), nullable=False),
        sa.Column("max_sweep", sa.Numeric(14, 2), nullable=False),
        sa.Column("max_weekly_sweep", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "min_days_between_sweeps", sa.Integer, nullable=False, server_default=sa.text("7")
        ),
        sa.Column(
            "blackout_dates",
            sa.ARRAY(sa.Date),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        # True when this change *loosened* a guardrail (lowered buffer_floor, raised a cap, shortened
        # spacing) — the audit marker for the deferred step-up retrofit (KTD-9). The route sets it.
        sa.Column("loosened", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("buffer_floor >= 0", name="ck_policy_events_buffer_nonneg"),
        sa.CheckConstraint("max_sweep >= 0", name="ck_policy_events_max_sweep_nonneg"),
        sa.CheckConstraint("max_weekly_sweep >= 0", name="ck_policy_events_weekly_nonneg"),
        sa.CheckConstraint(
            "min_days_between_sweeps >= 0", name="ck_policy_events_spacing_nonneg"
        ),
    )
    op.create_index("ix_policy_events_household_seq", _TABLE, ["household_id", "seq"])

    # Append-only: SELECT + INSERT and nothing else. A guardrail change is a new row, never a rewrite.
    op.execute(f"GRANT SELECT, INSERT ON {_TABLE} TO {APP_ROLE};")

    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY {_TABLE}_household_isolation ON {_TABLE}
            USING      (household_id = current_setting('{RLS_VAR}', true))
            WITH CHECK (household_id = current_setting('{RLS_VAR}', true));
        """
    )

    # The backfill — one initial event per existing household, before the mutable table is dropped.
    # `md5(random()::text || household_id)` is a stable-enough id; `changed_by` NULL (no user seeded
    # these), `loosened` false (an initial state loosens nothing). Runs as the migration owner, so it
    # is not subject to RLS.
    op.execute(
        f"""
        INSERT INTO {_TABLE}
            (id, household_id, changed_by, buffer_floor, max_sweep, max_weekly_sweep,
             min_days_between_sweeps, blackout_dates, loosened)
        SELECT
            'pe_backfill_' || household_id, household_id, NULL, buffer_floor, max_sweep,
            max_weekly_sweep, min_days_between_sweeps, blackout_dates, false
        FROM {_OLD};
        """
    )

    # The mutable upsert is retired in favour of the append (KTD-6). Its policy and grant go with it.
    op.execute(f"DROP POLICY IF EXISTS {_OLD}_household_isolation ON {_OLD};")
    op.drop_table(_OLD)


def downgrade() -> None:
    # Recreate the mutable policies table (mirrors 0001) and repopulate it from the latest event per
    # household, so a downgrade does not lose the current guardrails either.
    op.create_table(
        _OLD,
        sa.Column(
            "household_id",
            sa.Text,
            sa.ForeignKey("households.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("buffer_floor", sa.Numeric(14, 2), nullable=False),
        sa.Column("max_sweep", sa.Numeric(14, 2), nullable=False),
        sa.Column("max_weekly_sweep", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "min_days_between_sweeps", sa.Integer, nullable=False, server_default=sa.text("7")
        ),
        sa.Column(
            "blackout_dates", sa.ARRAY(sa.Date), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.CheckConstraint("min_days_between_sweeps >= 0", name="ck_policies_spacing_nonneg"),
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_OLD} TO {APP_ROLE};")
    op.execute(f"ALTER TABLE {_OLD} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {_OLD} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY {_OLD}_household_isolation ON {_OLD}
            USING      (household_id = current_setting('{RLS_VAR}', true))
            WITH CHECK (household_id = current_setting('{RLS_VAR}', true));
        """
    )
    op.execute(
        f"""
        INSERT INTO {_OLD}
            (household_id, buffer_floor, max_sweep, max_weekly_sweep, min_days_between_sweeps,
             blackout_dates)
        SELECT DISTINCT ON (household_id)
            household_id, buffer_floor, max_sweep, max_weekly_sweep, min_days_between_sweeps,
            blackout_dates
        FROM {_TABLE}
        ORDER BY household_id, seq DESC;
        """
    )

    op.execute(f"DROP POLICY IF EXISTS {_TABLE}_household_isolation ON {_TABLE};")
    op.drop_table(_TABLE)

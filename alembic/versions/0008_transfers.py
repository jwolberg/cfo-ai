"""transfers — the append-only ledger of money movement.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-17

Ticket 0039, the first unit of the sweep-execution rung
(`docs/plans/2026-07-17-002-feat-sweep-execution-rung-plan.md`). The write half: one row per state
transition of a transfer leg — a debit (ACH pull from checking into a platform funding account) or a
payoff (a biller-payoff API landing that money on the card).

Two things make append-only a guarantee rather than a hope, exactly as `plaid_transactions` (0036):

1. **The grant is SELECT, INSERT — and nothing else.** `cfo_app` cannot UPDATE or DELETE a row here,
   so no application path can rewrite the money-movement history even by mistake. A state transition
   is a new row.
2. **No UNIQUE on `provider_transfer_id`.** A later status-transition row (submitted → settled) and a
   superseding transfer (a same-day re-decision) both share it; a UNIQUE would reject the very rows
   the append-only design exists to keep.

The idempotency guard `architecture.md` [5] calls "the highest-stakes in the system" keys on
`(household_id, decision_date)` — not `decision_id` — and is enforced by a `SELECT … FOR UPDATE` slot
lock in the saga (U4), **not** a constraint here: a blanket UNIQUE would reject the append-only
corrections above. The two supporting indexes below make the slot lookup and the per-household scan
cheap.

The table name is a literal, not read from `HOUSEHOLD_SCOPED` (ticket 0033). `downgrade()` drops a
policy and a table and touches no role.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from backend.db.models import RLS_VAR
from backend.db.session import APP_ROLE

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

# Named literally, not read out of `HOUSEHOLD_SCOPED` — see 0001's note and ticket 0033.
_TABLE = "transfers"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Text, primary_key=True),
        # Monotonic insertion order — the total order the latest-state-per-slot query (U4/U5) reads.
        # `created_at` is transaction-start `now()` and ties within a saga step, so it cannot order
        # the state-machine transitions this ledger exists to record.
        sa.Column("seq", sa.BigInteger, sa.Identity(always=True), nullable=False),
        sa.Column(
            "household_id",
            sa.Text,
            sa.ForeignKey("households.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_card_id", sa.Text, nullable=False),
        sa.Column("decision_id", sa.Text, nullable=False),
        sa.Column("decision_date", sa.Date, nullable=False),
        sa.Column("leg", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("direction", sa.Text, nullable=False),
        # NUMERIC(14,2), never float (ADR-0002 [2.2]). A transfer always moves a positive amount.
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("provider", sa.Text, nullable=False),
        # NULL until `submit` returns; **not unique** — a superseding or status-transition row shares it.
        sa.Column("provider_transfer_id", sa.Text, nullable=True),
        sa.Column("return_code", sa.Text, nullable=True),
        sa.Column("idempotency_key", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("leg IN ('debit', 'payoff')", name="ck_transfers_leg"),
        sa.CheckConstraint(
            "state IN ('proposed', 'authorized', 'submitted', 'pending', 'settled',"
            " 'returned', 'failed', 'cancelled')",
            name="ck_transfers_state",
        ),
        sa.CheckConstraint("direction IN ('debit', 'credit')", name="ck_transfers_direction"),
        sa.CheckConstraint("provider IN ('increase', 'method')", name="ck_transfers_provider"),
        sa.CheckConstraint("amount > 0", name="ck_transfers_amount_positive"),
    )

    op.create_index("ix_transfers_household", _TABLE, ["household_id"])
    # The idempotency slot the saga locks with `SELECT … FOR UPDATE` (U4 / KTD-2).
    op.create_index("ix_transfers_slot", _TABLE, ["household_id", "decision_date"])

    # **SELECT and INSERT only** — no UPDATE, no DELETE. Append-only enforced at the privilege layer:
    # a state transition is a new row, and the database refuses to let the application rewrite an old
    # one. The one grant that matters most in the schema, because the row it protects is a debit.
    op.execute(f"GRANT SELECT, INSERT ON {_TABLE} TO {APP_ROLE};")

    # RLS exactly as 0001/0005/0006 — ENABLE and FORCE, USING and WITH CHECK on the household scope,
    # failing closed when the scope is unset. See 0001 for the full reasoning.
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY {_TABLE}_household_isolation ON {_TABLE}
            USING      (household_id = current_setting('{RLS_VAR}', true))
            WITH CHECK (household_id = current_setting('{RLS_VAR}', true));
        """
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {_TABLE}_household_isolation ON {_TABLE};")
    # The grant and the indexes go with the table.
    op.drop_table(_TABLE)

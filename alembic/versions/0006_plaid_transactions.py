"""plaid_transactions — the append-only landing table for /transactions/sync.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-17

Ticket 0036. See `docs/plans/2026-07-17-001-feat-plaid-transport-rung-plan.md`.

One row per `/transactions/sync` outcome. added, modified, and removed are **all INSERTs** — a
correction is a new row carrying a `change_type`, never an UPDATE or a DELETE (`architecture.md`
[4]). Two things make that a guarantee rather than a hope:

1. **The grant is SELECT, INSERT — and nothing else.** `cfo_app` cannot UPDATE or DELETE a row here,
   so no application path can rewrite history even by mistake. This is the one table whose grant
   differs from `0001`/`0004`/`0005`'s SELECT/INSERT/UPDATE/DELETE, and the difference is the point.
2. **No UNIQUE on `plaid_transaction_id`.** A legitimate second `modified` of the same transaction
   shares it; a UNIQUE constraint would reject the correction the append-only design exists to keep.

The table name is a literal, not read from `HOUSEHOLD_SCOPED` (ticket 0033). `downgrade()` drops a
policy and a table and touches no role.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from backend.db.models import RLS_VAR
from backend.db.session import APP_ROLE

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# Named literally, not read out of `HOUSEHOLD_SCOPED` — see 0001's note and ticket 0033.
_TABLE = "plaid_transactions"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "household_id",
            sa.Text,
            sa.ForeignKey("households.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("plaid_item_id", sa.Text, nullable=False),
        sa.Column("plaid_account_id", sa.Text, nullable=False),
        sa.Column("plaid_transaction_id", sa.Text, nullable=False),
        sa.Column("pending_transaction_id", sa.Text, nullable=True),
        # NUMERIC(14,2), never float (ADR-0002 [2.2]). Nullable: `removed` carries no amount.
        sa.Column("amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("date", sa.Date, nullable=True),
        sa.Column("name", sa.Text, nullable=True),
        sa.Column("merchant_name", sa.Text, nullable=True),
        sa.Column("change_type", sa.Text, nullable=False),
        sa.Column(
            "ingested_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "change_type IN ('added', 'modified', 'removed')",
            name="ck_plaid_transactions_change_type",
        ),
    )

    op.create_index("ix_plaid_transactions_household", _TABLE, ["household_id"])

    # **SELECT and INSERT only** — no UPDATE, no DELETE. Append-only enforced at the privilege layer:
    # a correction is a new row, and the database refuses to let the application rewrite an old one.
    op.execute(f"GRANT SELECT, INSERT ON {_TABLE} TO {APP_ROLE};")

    # RLS exactly as 0001/0005 — ENABLE and FORCE, USING and WITH CHECK on the household scope,
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
    # The grant and the index go with the table.
    op.drop_table(_TABLE)

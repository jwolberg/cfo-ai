"""plaid_accounts + plaid_liabilities — append-only balance and card-terms snapshots.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-18

The Link rung, Phase 2. `/transactions/sync` lands what a household *did* (`plaid_transactions`,
0006); these land what is *true right now* — the checking/savings balances and the card terms
(statement, due date, APR) that a transaction history cannot carry. Without them a linked household
has no `opening_balance` and no `CardSpec`, so `assemble_snapshot` has nothing to decide about.

**Append-only snapshots, read latest-by-`seq`.** A balance is a point-in-time fact that changes, but
we never *rewrite* one: each refresh is a new row, and the current balance is the newest. That is the
same append-only discipline as `plaid_transactions` and `policy_events`, and for the same two
reasons: the grant is `SELECT, INSERT` (no UPDATE/DELETE — the app cannot rewrite history even by
mistake), and the reader orders by a monotonic `seq`, never `fetched_at` — two refreshes in one
transaction can tie on the wall clock, and the state's whole meaning is which is newer (the
`transfers`/`policy_events` lesson).

RLS is ENABLE + FORCE with USING/WITH CHECK on the household scope, exactly as every household-scoped
table since 0001. `downgrade()` drops the policies and the tables and touches no role.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from backend.db.models import RLS_VAR
from backend.db.session import APP_ROLE

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

# Named literally, not read out of `HOUSEHOLD_SCOPED` — see 0001's note and ticket 0033.
_ACCOUNTS = "plaid_accounts"
_LIABILITIES = "plaid_liabilities"


def _scope(table: str) -> None:
    """The grant + RLS every household-scoped, append-only ingest table gets (as 0006)."""
    op.create_index(f"ix_{table}_household", table, ["household_id"])
    # SELECT and INSERT only — a refresh is a new row; the database refuses an UPDATE or DELETE.
    op.execute(f"GRANT SELECT, INSERT ON {table} TO {APP_ROLE};")
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY {table}_household_isolation ON {table}
            USING      (household_id = current_setting('{RLS_VAR}', true))
            WITH CHECK (household_id = current_setting('{RLS_VAR}', true));
        """
    )


def upgrade() -> None:
    op.create_table(
        _ACCOUNTS,
        sa.Column("id", sa.Text, primary_key=True),
        # Monotonic order — the latest snapshot per account is read by this, never by `fetched_at`.
        sa.Column("seq", sa.BigInteger, sa.Identity(always=True), nullable=False),
        sa.Column(
            "household_id",
            sa.Text,
            sa.ForeignKey("households.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("plaid_item_id", sa.Text, nullable=False),
        sa.Column("plaid_account_id", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=True),
        sa.Column("official_name", sa.Text, nullable=True),
        # Plaid's `type`/`subtype`: depository/checking, depository/savings, credit/credit card.
        sa.Column("type", sa.Text, nullable=True),
        sa.Column("subtype", sa.Text, nullable=True),
        # NUMERIC(14,2), never float (ADR-0002 [2.2]). `current` is what the engine reads as the
        # balance; `available` can be null (a credit card has no "available" in the same sense).
        sa.Column("current_balance", sa.Numeric(14, 2), nullable=True),
        sa.Column("available_balance", sa.Numeric(14, 2), nullable=True),
        sa.Column("iso_currency_code", sa.Text, nullable=True),
        sa.Column(
            "fetched_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    _scope(_ACCOUNTS)

    op.create_table(
        _LIABILITIES,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("seq", sa.BigInteger, sa.Identity(always=True), nullable=False),
        sa.Column(
            "household_id",
            sa.Text,
            sa.ForeignKey("households.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("plaid_item_id", sa.Text, nullable=False),
        sa.Column("plaid_account_id", sa.Text, nullable=False),
        # The card terms `CardSpec` needs. All nullable — Plaid returns what the issuer reports, and
        # an unknown APR is exactly the `apr_source='estimated'` case ticket 0028 exists for.
        sa.Column("last_statement_balance", sa.Numeric(14, 2), nullable=True),
        sa.Column("last_statement_issue_date", sa.Date, nullable=True),
        sa.Column("minimum_payment", sa.Numeric(14, 2), nullable=True),
        sa.Column("next_payment_due_date", sa.Date, nullable=True),
        # The purchase APR as a fraction (0.2399), not a percentage — the engine's `apr` convention
        # (`engine/models.py`). The fetch converts Plaid's percentage on the way in.
        sa.Column("purchase_apr", sa.Numeric(6, 5), nullable=True),
        sa.Column("is_overdue", sa.Boolean, nullable=True),
        sa.Column(
            "fetched_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "purchase_apr IS NULL OR (purchase_apr >= 0 AND purchase_apr <= 2)",
            name="ck_plaid_liabilities_apr_range",
        ),
    )
    _scope(_LIABILITIES)


def downgrade() -> None:
    for table in (_LIABILITIES, _ACCOUNTS):
        op.execute(f"DROP POLICY IF EXISTS {table}_household_isolation ON {table};")
        op.drop_table(table)

"""plaid_items — the Plaid transport rung's first table.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-17

Ticket 0034. See `docs/plans/2026-07-17-001-feat-plaid-transport-rung-plan.md`.

One row per linked Plaid Item. HOUSEHOLD_SCOPED and RLS-FORCEd exactly like `0001`/`0004` — the
`access_token` is a credential, and every credential in this schema is scoped by household. Two
things worth reading before touching this:

1. **The table name is a literal, never `from backend.db.models import HOUSEHOLD_SCOPED`.** That
   is the whole point of ticket `0033`: a migration states what *it* did, and importing the live
   constant makes an old revision describe today's schema. Adding `plaid_items` takes the scoped
   count from six to seven; this revision names only its own table.
2. **`downgrade()` drops a policy and a table and nothing else.** It does not touch the `cfo_app`
   role — a role is cluster-wide and this migration is database-scoped (`0001`'s trap), and it
   names no database it never created (`0020`'s trap).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from backend.db.models import RLS_VAR
from backend.db.session import APP_ROLE

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# Named literally, not read out of `HOUSEHOLD_SCOPED` — see the module docstring and ticket 0033.
_TABLE = "plaid_items"


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
        sa.Column("institution_id", sa.Text, nullable=True),
        # Plaintext in Sandbox; `backend/db/session.py`'s start guard forbids a non-sandbox boot
        # while it stays that way. KMS envelope encryption lands with the first real token.
        sa.Column("access_token", sa.Text, nullable=False),
        # The `/transactions/sync` cursor, persisted per item. NULL before the first sync.
        sa.Column("cursor", sa.Text, nullable=True),
        # engine.models.ConnectionState. Ships `healthy`.
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'healthy'")),
        # Becomes `Account.balance_age_days`, the freshness gate. NULL until the first success.
        sa.Column("last_successful_sync_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error_code", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('healthy', 'login_required', 'disconnected')",
            name="ck_plaid_items_status",
        ),
    )

    # Plaid's Item id is unique across the whole deployment — it is Plaid's key, and the webhook
    # doorbell (0035) resolves `plaid_item_id → household_id` through it. A named constraint so the
    # doorbell's later ON CONFLICT / lookup has a stable name to rely on.
    op.create_unique_constraint("uq_plaid_items_plaid_item_id", _TABLE, ["plaid_item_id"])
    op.create_index("ix_plaid_items_household", _TABLE, ["household_id"])

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO {APP_ROLE};")

    # Both halves of the policy, FORCEd — the reasoning is `0001`'s. USING governs what is visible,
    # WITH CHECK governs what may be written; without the second a session scoped to A could INSERT
    # a row owned by B. `current_setting(RLS_VAR, true)` is NULL when unset and NULL never equals a
    # household_id, so an unscoped query sees zero rows: RLS fails closed.
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
    # The grant, the unique constraint, and the index go with the table.
    op.drop_table(_TABLE)

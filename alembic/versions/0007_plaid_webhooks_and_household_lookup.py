"""plaid_webhooks (unscoped) and the SECURITY DEFINER household lookup.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-17

Ticket 0035, and the schema half of ADR-0005. This revision opens the two — and only two —
exceptions to "every table is scoped by household_id" (`architecture.md` [4]):

1. **`plaid_webhooks`, unscoped.** Plaid's webhook carries `item_id`, not `household_id`, so the
   doorbell cannot resolve a household before RLS could bind — and RLS fails closed, so a scoped
   INSERT here would match nothing and reject the row. The raw store is therefore outside
   `HOUSEHOLD_SCOPED`, mirroring `GET /households`. The dedup key is **NULLS NOT DISTINCT** because
   the webhook that matters most (TRANSACTIONS / SYNC_UPDATES_AVAILABLE) carries no cursor: under
   default NULL-distinct semantics two redeliveries would both be admitted, the opposite of dedup.

2. **`plaid_household_for_item(text) RETURNS text`, SECURITY DEFINER.** The worker (0037) must read
   `plaid_items` to map `plaid_item_id → household_id` *before* it can set a scope — but `plaid_items`
   is FORCE'd, so even the table owner sees nothing without `app.household_id`. This function runs as
   its owner (the migration-runner: a superuser or BYPASSRLS role in every environment that runs
   migrations) and so bypasses RLS — but only within this one narrow, audited surface that returns a
   single `household_id`. That is the whole reason it is preferred over granting `cfo_app` BYPASSRLS,
   which would unscope *every* query the application makes. `households.id` is `text` in this schema
   (not the `uuid` the plan sketched), so the function returns `text`.

`downgrade()` drops the function and the table and touches no role.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from backend.db.session import APP_ROLE

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_TABLE = "plaid_webhooks"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("plaid_item_id", sa.Text, nullable=False),
        sa.Column("webhook_type", sa.Text, nullable=False),
        sa.Column("webhook_code", sa.Text, nullable=False),
        sa.Column("cursor", sa.Text, nullable=True),
        sa.Column("payload", sa.dialects.postgresql.JSONB, nullable=False),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # NULLS NOT DISTINCT so a NULL cursor still dedups (Postgres 15+; CI is 16, deploy is Neon).
    # Written as raw DDL because SQLAlchemy's UniqueConstraint has no NULLS NOT DISTINCT option.
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT uq_plaid_webhooks_dedup "
        f"UNIQUE NULLS NOT DISTINCT (plaid_item_id, webhook_code, cursor);"
    )
    op.create_index("ix_plaid_webhooks_item", _TABLE, ["plaid_item_id"])

    # The doorbell inserts (ON CONFLICT DO NOTHING) and reads; retention purges old rows (ADR-0005).
    # No UPDATE: a received payload is immutable. **No RLS** — this table is deliberately unscoped.
    op.execute(f"GRANT SELECT, INSERT, DELETE ON {_TABLE} TO {APP_ROLE};")

    # The one sanctioned way to read plaid_items before a scope is set. SECURITY DEFINER runs as the
    # owner; `SET search_path` is pinned so the definer body cannot be hijacked by a caller's path.
    op.execute(
        """
        CREATE FUNCTION plaid_household_for_item(p_plaid_item_id text)
        RETURNS text
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT household_id FROM plaid_items WHERE plaid_item_id = p_plaid_item_id;
        $$;
        """
    )
    # EXECUTE only — cfo_app calls it, it does not own it, so it inherits the definer's bypass only
    # for this one lookup and nothing else.
    op.execute(f"GRANT EXECUTE ON FUNCTION plaid_household_for_item(text) TO {APP_ROLE};")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS plaid_household_for_item(text);")
    op.drop_table(_TABLE)

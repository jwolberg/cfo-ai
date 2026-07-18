"""transfer_webhooks (unscoped) and the SECURITY DEFINER household lookup for the saga.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-17

Ticket 0042, U4 of the sweep-execution rung, and the schema half of ADR-0006. The write half's saga
faces the same tenancy problem the Plaid doorbell did (ADR-0005), so it reuses that shape:

1. **`transfer_webhooks`, unscoped.** Increase and Method webhooks name a provider transfer id, not a
   household — and `transfers` is FORCE'd, so a scoped INSERT here would match nothing and be
   rejected before a scope can be set. The raw store is therefore outside `HOUSEHOLD_SCOPED`,
   mirroring `plaid_webhooks`. The dedup key is **NULLS NOT DISTINCT** because a status webhook may
   carry no distinct event id, and two redeliveries must collapse rather than both admit.

2. **`transfer_household_for_provider_ref(text, text) RETURNS text`, SECURITY DEFINER.** The worker
   must map `(provider, provider_transfer_id) → household_id` before it can scope. Runs as its owner
   over the one narrow surface, returns a single household id and nothing else — never `BYPASSRLS`
   on `cfo_app`, which would unscope every query (ADR-0006, and ADR-0005 [3]'s rule that a new
   definer function is a new ADR).

`downgrade()` drops the function and the table and touches no role.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from backend.db.session import APP_ROLE

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_TABLE = "transfer_webhooks"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("provider", sa.Text, nullable=False),
        # The provider's transfer/payment id the event is about. NULL for an event that names none.
        sa.Column("provider_transfer_id", sa.Text, nullable=True),
        sa.Column("event_type", sa.Text, nullable=False),
        # The vendor's own event id, when it carries one — the dedup key. NULL dedups too.
        sa.Column("event_id", sa.Text, nullable=True),
        sa.Column("payload", sa.dialects.postgresql.JSONB, nullable=False),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # NULLS NOT DISTINCT so a NULL event_id still dedups (Postgres 15+; CI is 16, deploy is Neon).
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT uq_transfer_webhooks_dedup "
        f"UNIQUE NULLS NOT DISTINCT (provider, event_id);"
    )
    op.create_index("ix_transfer_webhooks_ref", _TABLE, ["provider", "provider_transfer_id"])

    # Insert (ON CONFLICT DO NOTHING) + read; retention purges old rows. No UPDATE, **no RLS** — the
    # store is deliberately unscoped, exactly like plaid_webhooks.
    op.execute(f"GRANT SELECT, INSERT, DELETE ON {_TABLE} TO {APP_ROLE};")

    # The one sanctioned read of FORCE'd `transfers` before a scope is set. `search_path` pinned so
    # the definer body cannot be hijacked. LIMIT 1: the append-only ledger has many rows per transfer,
    # all carrying the same household_id.
    op.execute(
        """
        CREATE FUNCTION transfer_household_for_provider_ref(p_provider text, p_ref text)
        RETURNS text
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT household_id FROM transfers
            WHERE provider = p_provider AND provider_transfer_id = p_ref
            LIMIT 1;
        $$;
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION transfer_household_for_provider_ref(text, text) "
        f"TO {APP_ROLE};"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS transfer_household_for_provider_ref(text, text);")
    op.drop_table(_TABLE)

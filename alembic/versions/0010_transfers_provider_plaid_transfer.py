"""transfers.provider admits plaid_transfer (the primary debit rail, ADR-0007).

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-18

Ticket 0045. ADR-0007 makes **Plaid Transfer** the primary debit rail, with Increase kept as a
swappable backup. The `transfers.provider` CHECK (from 0008) admitted only `increase`/`method`; this
widens it to `plaid_transfer`/`increase`/`method` — the backup stays valid, nothing is removed. A
CHECK swap is a drop-and-recreate; no data changes (the rung has never run live).
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_TABLE = "transfers"
_CK = "ck_transfers_provider"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {_CK};")
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CK} "
        f"CHECK (provider IN ('plaid_transfer', 'increase', 'method'));"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {_CK};")
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CK} CHECK (provider IN ('increase', 'method'));"
    )

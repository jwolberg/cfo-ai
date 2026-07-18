"""card_attestations — the append-only record that clears the 0016 money-gate.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-18

Ticket 0050 (the identity rung's U5), KTD-7. The engine refuses to sweep a household whose card set
it cannot be sure is complete (`CoverageState.UNATTESTED` → `CARD_COVERAGE_INCOMPLETE`,
`engine/decide.py`). Until now `assemble_snapshot()` hardcoded `attested=True`, so nobody had ever
cleared that gate end to end (ticket 0016). This table is the real record a user's attestation lands
in.

`card_fingerprint` is a stable hash of the attested card set. Coverage is `COMPLETE` only when a
current attestation's fingerprint matches the household's **current** cards — so a newly appearing
card changes the fingerprint and silently drops the household back to `UNATTESTED`, which is the
correct safety behaviour. Attestation only ever moves `UNATTESTED → COMPLETE`; it never overrides
`UNMATCHED_PAYMENT` (that override lives in `derive_portfolio`).

Append-only, exactly like `policy_events` and `transfers`: `cfo_app` is granted SELECT + INSERT and
nothing else, so an attestation is a new row and never a rewrite. Table names are literals (0033).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from backend.db.models import RLS_VAR
from backend.db.session import APP_ROLE

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_TABLE = "card_attestations"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Text, primary_key=True),
        # Monotonic order — `current_attestation()` reads the latest by this, not `created_at`
        # (a seed transaction can tie on the clock; the `transfers.seq` lesson).
        sa.Column("seq", sa.BigInteger, sa.Identity(always=True), nullable=False),
        sa.Column(
            "household_id",
            sa.Text,
            sa.ForeignKey("households.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The user who attested. NULL for a seeded/fixture attestation; ON DELETE SET NULL so a
        # shredded user's attestation history survives with the actor nulled.
        sa.Column(
            "attested_by",
            sa.Text,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # A stable hash of the attested card identities. Coverage is COMPLETE only while this matches
        # the current card set's fingerprint.
        sa.Column("card_fingerprint", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_card_attestations_household_seq", _TABLE, ["household_id", "seq"])

    # Append-only: SELECT + INSERT and nothing else. An attestation is a new row, never a rewrite.
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


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {_TABLE}_household_isolation ON {_TABLE};")
    op.drop_table(_TABLE)

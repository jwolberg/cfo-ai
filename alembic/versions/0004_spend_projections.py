"""The spend projection — the half of the surface ingest has not built yet.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-16

Ticket 0031. See `backend/spend.py`, which is where the reasoning lives.

`/spend` was the last route reading `backend/data/decisions.json`. Moving it to Postgres needs
figures derived from the full transaction `History` — every overlapping 30-day total by channel,
and what each card took last cycle against what came off it — and **there is no `transactions`
table**. That is ingest (`architecture.md` [3.1]) and it is not built.

So this table holds a **projection**: derived once by the seeder from the history it walked, read
back by the read path, and **deleted by ingest** when the real series can be computed from real
transactions. It is stored knowingly rather than quietly, which is what `0031`'s "out of scope"
note asked for.

## What is deliberately not in here

Each card's statement, unbilled balance, and the reserve held against it are **not** stored. They
come off the frozen `Snapshot` on every request — `untouchable()`'s own per-card terms, via
`backend/spend.py`. A stored copy would duplicate what `snapshots.payload` already holds and be
free to disagree with it about what the engine saw. `architecture.md` [4.1] is a whole section
about not restructuring storage on a guess, and `readpath.py` makes the same argument against
denormalizing display fields onto `decisions`.

That split is the whole design: store only what cannot be derived, and derive the rest.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from backend.db.models import RLS_VAR
from backend.db.session import APP_ROLE

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# This revision's own table, named literally rather than read out of `HOUSEHOLD_SCOPED` — see the
# note at the top of `0001`, which this ticket is the first change to have broken. A migration
# states what *it* did; the application constant states what must be scoped now.
_TABLE = "spend_projections"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        # `household_id` is the primary key: one projection per household, describing the last day
        # served. Not keyed by day — this is not a series, it is a single rendered surface, and a
        # row per day would be a second decision log made of numbers nothing decides on.
        sa.Column(
            "household_id",
            sa.Text,
            sa.ForeignKey("households.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("as_of", sa.Date, nullable=False),
        sa.Column("payload", sa.dialects.postgresql.JSONB, nullable=False),
    )

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO {APP_ROLE};")

    # Both halves of the policy, for the reason `0001` spells out: USING governs what is visible,
    # WITH CHECK governs what may be written, and without the second a household scoped to A could
    # INSERT a row owned by B. FORCEd because a policy the owner is exempt from is decoration.
    #
    # `current_setting(RLS_VAR, true)` returns NULL rather than raising when unset, and NULL never
    # equals household_id — so an unscoped query sees zero rows. RLS fails closed.
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
    # The grant goes with the table. `0001`'s `DROP OWNED BY` is what cleans up anything left, and
    # the role itself is deliberately not dropped there — a role is cluster-wide and this migration
    # is database-scoped.
    op.drop_table(_TABLE)

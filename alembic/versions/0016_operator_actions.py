"""operator_actions — the append-only record every operator override is written to.

The Admin lane of `docs/status.html` promises that a pause or a halt is "logged to the same
append-only record as a decision". This is that record: who did what, to which household (or all of
them), and when. It is platform-level, not household-scoped — a global halt belongs to no household,
and the operator is not a household member — so it carries **no RLS**, the same posture as `users`
(0011, KTD-1). The discipline that replaces RLS is that it is **append-only**: the app role may
`SELECT` and `INSERT` and nothing else, so an override cannot be un-recorded after the fact.

Two states are *derived* from this log rather than stored a second time (the store-only-what-cannot-
be-derived rule): a household's pause state is `policy_events.blackout_dates` (the system of record
for guardrails), and the global-halt state is the latest `halt`/`resume` row here. Nothing keeps a
mutable "is_halted" flag that could disagree with the log that explains it.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op
from backend.db.session import APP_ROLE

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_TABLE = "operator_actions"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("seq", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column(
            "at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        # The operator who acted. A plain string, not a users FK: the operator is a god-mode
        # credential, not a customer identity — real per-operator identity is the still-to-come
        # `Access & audit` box, and when it lands this column is where it attaches.
        sa.Column("actor", sa.Text, nullable=False),
        # 'pause' | 'unpause' | 'halt' | 'resume'. A CHECK keeps the vocabulary closed so a typo
        # cannot invent a fifth action the reader has to guess at.
        sa.Column("action", sa.Text, nullable=False),
        # The household acted on; NULL for a global halt/resume, which belongs to no one household.
        sa.Column("household_id", sa.Text, nullable=True),
        sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_check_constraint(
        f"{_TABLE}_action_vocab",
        _TABLE,
        "action IN ('pause', 'unpause', 'halt', 'resume')",
    )
    # A global halt/resume names no household; a pause/unpause must. Encoded so a mislabelled row
    # cannot land — the log is only as trustworthy as the rows it refuses.
    op.create_check_constraint(
        f"{_TABLE}_household_presence",
        _TABLE,
        "(action IN ('halt', 'resume')) = (household_id IS NULL)",
    )
    op.create_index(f"ix_{_TABLE}_at", _TABLE, ["at"])

    # SELECT + INSERT only. No UPDATE, no DELETE: the record is append-only, so even the app role
    # that writes it cannot rewrite or erase an override once it is logged. A GENERATED ... AS
    # IDENTITY column needs no separate sequence grant — INSERT on the table covers it.
    op.execute(f"GRANT SELECT, INSERT ON {_TABLE} TO {APP_ROLE};")


def downgrade() -> None:
    op.drop_table(_TABLE)

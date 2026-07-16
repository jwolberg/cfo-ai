"""cards.apr_source — where the rate came from.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-16

Ticket 0028. Settles `architecture.md` §7.5, one of the five decisions it said must land before
the first migration — so it lands in the second, which is close enough to honest given 0001 was
written before the decision was made.

**The confidence has to be stored or it is not reduced, it is merely absent.** A 23% estimate and
a reported 23% are the same number; only this column tells them apart, and `engine/interest.py`
reads it to decide whether it may compute the figure `prd.md` §5.1 grades the company on.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `server_default='reported'` so the column is backfillable on a live table without a
    # rewrite, and so any row written before this migration reads as what it was: a rate an
    # issuer actually gave us.
    op.add_column(
        "cards",
        sa.Column("apr_source", sa.Text, nullable=False, server_default=sa.text("'reported'")),
    )
    op.create_check_constraint(
        "ck_cards_apr_source",
        "cards",
        "apr_source IN ('reported', 'user_entered', 'estimated')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_cards_apr_source", "cards", type_="check")
    op.drop_column("cards", "apr_source")

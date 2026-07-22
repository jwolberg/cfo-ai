"""users.is_demo — the demo plane becomes a property of the identity, not of the route.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-21

Ticket 0058. `households.is_demo` (0011) marks which *data* is the public demo plane, and the
`owner`/`viewer` role refuses the demo viewer every household-scoped write. But role is per-household,
so a route with **no household to authorize against** has no role to check — and `POST /households`
is exactly that route. The public demo viewer could create a real (`is_demo = false`) household, be
made its owner, and thereby re-open `PATCH /policy`, `POST /attest`, and `POST /plaid/link/exchange`
on it. Measured in production 2026-07-21: `201 Created`.

The fix is to mark the *identity*, so "may this caller reach the real plane at all?" is answerable
before any household exists. `is_demo = false` by default, so a real user is never a demo one by
omission — the same default-safe discipline 0011 used for `households.is_demo`.

**Backfill: a user whose every membership is a demo household is a demo user.** That is the honest
definition of "lives on the demo plane" and it does not depend on an env var (`DEMO_STYTCH_EMAIL`),
which would put the safety property back into deploy discipline — the thing ticket 0057 chose its
lane to avoid. A user with no memberships at all is left `false`: they have not been placed on the
demo plane, and defaulting a brand-new JIT-provisioned user to `true` would lock real users out.

No RLS: `users` is deliberately platform-level (0011, KTD-1), and the existing
`GRANT SELECT, INSERT, UPDATE` already covers the new column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_USERS = "users"


def upgrade() -> None:
    op.add_column(
        _USERS,
        sa.Column("is_demo", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )

    # A user is a demo user when they have at least one membership and every one of them is to a
    # demo household. EXISTS/NOT EXISTS rather than a COUNT comparison so the semantics survive a
    # user who is a member of nothing (excluded by the first clause, not silently swept in).
    op.execute(
        f"""
        UPDATE {_USERS} u SET is_demo = true
        WHERE EXISTS (
                  SELECT 1 FROM household_members m JOIN households h ON h.id = m.household_id
                  WHERE m.user_id = u.id AND h.is_demo
              )
          AND NOT EXISTS (
                  SELECT 1 FROM household_members m JOIN households h ON h.id = m.household_id
                  WHERE m.user_id = u.id AND NOT h.is_demo
              );
        """
    )


def downgrade() -> None:
    op.drop_column(_USERS, "is_demo")

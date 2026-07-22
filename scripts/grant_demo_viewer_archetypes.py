"""Give the public demo viewer read access to the seeded archetype households.

The four archetypes have been in production since 2026-07-16 with a full 90-day graded feed each
(5-11 sweeps), but the demo viewer has no membership rows to them, so `GET /households` never lists
them and the public demo opens on `hh_demo_plaid` — a linked household with no graded window, whose
live decision is `card_behavior_unknown`. A refusal is the first and only thing a visitor sees.

Three rows fix that, as `viewer`. No code, no redeploy, no bundle rebuild: `Dashboard.tsx` already
renders a seeded household through its 90-day feed and reserves the live path for linked ones.

**`hh_apr_unreported` is deliberately excluded.** `readpath.list_households` orders by id and
`mobile/App.tsx` defaults to `households[0]`, so including it would make it the default household —
and "APR unreported" is the weakest opening frame of the four. Adding it later is one more row.

Every household written here is `is_demo = true`, so the demo viewer's `users.is_demo` stays true
(the 0058 gate reads the row, but the backfill's definition — all memberships demo — remains
satisfied, which matters if the backfill is ever re-derived).

**Re-run this after any `backend.seed.seed_all`.** That function deletes and recreates each
household, and `household_members` is `ON DELETE CASCADE`, so a re-seed drops these rows — and
`_seed_memberships` then re-grants them to `seed.py`'s own `DEMO_USER_ID` (`user_demo_viewer`),
which is *not* the identity production runs on. Production's demo viewer is the Stytch-reconciled
user `scripts/seed_demo_household.py` provisions from `DEMO_STYTCH_EMAIL`. The failure is silent:
the households still exist, the picker just goes back to showing one. Idempotent (`ON CONFLICT DO
NOTHING`), so running it when it is not needed costs nothing.

    DATABASE_URL="$(security find-generic-password -w -s cfoai-NEON-OWNER)" \
      .venv/bin/python <this file>
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine, text

DEMO_EMAIL = "demo-viewer@example.com"

# Sorted by id, which is the order the picker shows and the order the default is taken from.
GRANT = ("hh_demo_biweekly", "hh_monthly_thin", "hh_semimonthly_portfolio")


def main() -> None:
    engine = create_engine(os.environ["DATABASE_URL"])

    with engine.connect() as c, c.begin():
        user = c.execute(
            text("select id, is_demo from users where email = :e"), {"e": DEMO_EMAIL}
        ).one_or_none()
        if user is None:
            raise SystemExit(f"REFUSING: no user with email {DEMO_EMAIL}")
        if not user.is_demo:
            raise SystemExit(f"REFUSING: {user.id} is not flagged is_demo — wrong user.")

        for household_id in GRANT:
            row = c.execute(
                text("select is_demo from households where id = :h"), {"h": household_id}
            ).one_or_none()
            if row is None:
                raise SystemExit(f"REFUSING: household {household_id} does not exist")
            if not row.is_demo:
                # A non-demo membership would flip the viewer off the demo plane under the 0058
                # backfill's own definition. Never grant one here.
                raise SystemExit(f"REFUSING: {household_id} is not a demo household")

            c.execute(
                text(
                    "insert into household_members (household_id, user_id, role) "
                    "values (:h, :u, 'viewer') on conflict do nothing"
                ),
                {"h": household_id, "u": user.id},
            )

        listed = [
            r.id
            for r in c.execute(
                text(
                    "select h.id from households h"
                    " join household_members m on m.household_id = h.id"
                    " where m.user_id = :u order by h.id"
                ),
                {"u": user.id},
            )
        ]

    print(f"demo viewer {user.id} now sees {len(listed)} households, in picker order:")
    for i, h in enumerate(listed):
        print(f"  {h}{'   <-- default' if i == 0 else ''}")


if __name__ == "__main__":
    main()

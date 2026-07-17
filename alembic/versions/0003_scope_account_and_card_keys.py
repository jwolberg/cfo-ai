"""accounts and cards are keyed by household, not globally.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-16

Ticket 0023, found by trying to seed a second household.

`accounts.id` and `cards.id` were global primary keys, and every household the walk derives carries
the *same* account ids — `CHECKING_ID = "chk_demo"` and `SAVINGS_ID = "sav_demo"` are module
constants in `backend/precompute.py`, baked into `assemble_snapshot`'s `funding_account_id`. So the
first household seeds and the second raises `duplicate key value violates unique constraint
"accounts_pkey"`. Nothing had ever written a second one.

**The schema was already inconsistent about this and nobody had cause to notice.** `decisions` is
keyed `(household_id, day, id)`. `snapshots` hand-namespaces its id as `f"{household_id}:{day}"` to
get the same effect. `policies` is keyed by `household_id` outright. Only `accounts` and `cards`
assumed a global id space, which is an assumption inherited from a database that held exactly one
household.

It also bites something bigger than the archetypes: `prd.md` §5.2's population — the 60 households
`calibrate.py` measures and the whole basis of "a guardrail measured on one household is not
measured" — is 60 `DEMO_SPEC` clones, every one of them holding `card_demo` and `chk_demo`. Under a
global key that population is not merely unseeded, it is **unseedable**.

Scoping the key says the true thing: an account id is unique *within* a household. Every read is
already household-scoped, at the repository and again at RLS (`architecture.md` [4]), so nothing
resolves an id without a household to resolve it in. This also removes a cross-tenant coupling that
had no business existing — under a global key, one household's account id could collide with
another's, and the failure would surface as an insert error on an unrelated tenant's write.
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# Nothing references either id by foreign key — `decisions.target_card_id` is plain text, because a
# partitioned table cannot cheaply be the referencing side of one. So these swap without touching
# any dependent constraint.
_SCOPED = ("accounts", "cards")


def upgrade() -> None:
    for table in _SCOPED:
        op.drop_constraint(f"{table}_pkey", table, type_="primary")
        op.create_primary_key(f"{table}_pkey", table, ["household_id", "id"])


def downgrade() -> None:
    # Reversible only while no two households share an id. That is exactly the state this migration
    # exists to permit, so a downgrade after seeding more than one archetype will fail on the
    # duplicate — correctly, and loudly, rather than by discarding a household.
    for table in _SCOPED:
        op.drop_constraint(f"{table}_pkey", table, type_="primary")
        op.create_primary_key(f"{table}_pkey", table, ["id"])

"""Seed a household into Postgres by walking it — the walk's third consumer.

`walk()`'s own docstring names three callers: `build()` assembles an artifact, `replay()` grades,
"and a seeder writes them." This is that seeder, and it re-implements none of the stepping. If you
find yourself writing a ledger loop in this file, stop — that is the defect ticket `0019` exists
to close, and it had already happened twice before anyone noticed.

**It writes through `0021`'s repository, not around it.** A bulk `COPY` would be faster and would
prove nothing: the point is that the seeder exercises the same path a live daily job would, so the
code that eventually moves money is the code the demo has been running all along.

## What this cannot use, and why

`build()` is not an option. It raises on a multi-card spec by design (ticket `0027`) because
`DayRecord` carries one `debt_balance`, one `debt_apr`, one `debt_id` — the artifact schema has
exactly one debt. Three of the four archetypes hold a portfolio. So this drives `walk()` directly,
which carries `debt_balances` as a `Mapping[str, Decimal]` precisely so that re-introducing the
single-card assumption is a type error rather than a household that quietly does not exist.

## The one number that must not move

`generate(spec, start, days=WARMUP_DAYS + SERVED_DAYS)` — **150**, exactly what `build()` passes.
Not "about 150", and not a parameter with a friendlier default.

`sim.household.generate()` is **not prefix-stable**: it draws payroll and bills before discretionary
spend from one RNG stream, so `days` is part of the household's *identity* rather than a window onto
it. `(spec, seed, days=150)` and `(spec, seed, days=181)` are different households. Archetype A's
seeded decisions must equal the committed `backend/data/decisions.json` day for day — so this walks
the household `build()` walked, or the regression oracle silently compares two strangers and passes
for the wrong reason. Pinned in `tests/test_precompute.py::TestGenerateIsNotPrefixStable`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from backend.codec import encode_tree
from backend.db.repository import Repository, repository
from backend.db.snapshots import PostgresSnapshotStore
from backend.precompute import (
    SEED,
    SERVED_DAYS,
    WARMUP_DAYS,
    WINDOW_START,
    derive_spend_projection,
    walk,
)
from engine.models import Action
from sim.household import History, HouseholdSpec, generate

# What produced these decisions. The column is NOT NULL and `architecture.md` [3.3] rests real
# weight on it — "replay any new engine version across every historical snapshot and ask: would
# this have overdrafted anyone?" — so something has to be written here.
#
# **Nothing bumps this yet, and that is worth knowing rather than hiding.** There is no version
# constant anywhere in `engine/`; the artifact's `version: 3` is the *schema*'s, a different thing.
# It is a literal rather than a git SHA on purpose: the determinism this file promises is
# "re-seeding produces identical rows", and a SHA would make every commit rewrite the decision log
# of households that did not change.
#
# The day `engine/decide.py`'s logic changes without this moving, [3.3]'s backtest guarantee
# becomes a story — every row would claim to have been decided by a version that no longer exists.
ENGINE_VERSION = "0-unversioned"

# The two seeded identity principals (ticket 0046, KTD-10). These are the deployed-surface
# mitigation, not just a test fixture: after the shared key retires (U3), the public demo runs on
# the DEMO principal and a reviewer runs on the REVIEWER one.
#
# - **DEMO is a `viewer`** — the public, read-only demo principal. A visitor reaches the demo bundle
#   with no login and can read every `is_demo` household and write none. Its `stytch_user_id` is a
#   fixed sentinel because there is no real Stytch user behind the public session.
# - **REVIEWER is an `owner`** — a dev/reviewer who may exercise the write paths (settings,
#   attestation) against the demo households, and who (being a member of all of them) still sees the
#   HouseholdPicker.
#
# Both are members of every seeded (synthetic, `is_demo`) household — see `_seed_memberships`.
DEMO_USER_ID = "user_demo_viewer"
DEMO_USER_STYTCH_ID = "stytch-demo-viewer"
DEMO_USER_EMAIL = "demo@cfo-ai.example"

REVIEWER_USER_ID = "user_reviewer_owner"
REVIEWER_USER_STYTCH_ID = "stytch-reviewer-owner"
REVIEWER_USER_EMAIL = "reviewer@cfo-ai.example"


class SeedError(RuntimeError):
    """A household could not be seeded. Raised at the boundary, before anything is written."""


@dataclass(frozen=True)
class Seeded:
    """What a seed run wrote. Returned rather than printed, so tests can assert on it."""

    household_id: str
    archetype: str
    days: int
    sweeps: int
    refusals: int


def household_id_for(archetype: str) -> str:
    """Stable, readable, and derived — never random.

    Re-seeding must produce identical rows (ADR-0002's staleness property, re-pointed at the
    database), and a uuid4 here would make every run a different household while looking correct.
    """
    return f"hh_{archetype}"


def seed(
    engine: Engine,
    archetype: str,
    spec: HouseholdSpec,
    *,
    seed_value: int = SEED,
    start: date = WINDOW_START,
    warmup_days: int = WARMUP_DAYS,
    served_days: int = SERVED_DAYS,
) -> Seeded:
    """Walk one archetype and write its served window: decisions, snapshots, and current state.

    Idempotent by construction: the household is deleted and rewritten, so re-seeding converges
    rather than accumulating. `households.id` is derived from the archetype name, so "re-seed" and
    "seed again" are the same operation.
    """
    total_days = warmup_days + served_days
    household_id = household_id_for(archetype)

    history = generate(spec, start=start, days=total_days, seed=seed_value)

    # The household row itself cannot go through the scoped repository, and the reason is not an
    # oversight: `Repository` binds a `household_id` it filters *by*, and `households` is not one of
    # the RLS-scoped tables (`models.HOUSEHOLD_SCOPED`). You cannot scope a query to a household
    # that does not exist yet. Creating one is the boundary, so it happens here, once, in the same
    # transaction as everything it owns.
    with engine.begin() as conn:
        _reset_household(conn, household_id, archetype)
        # The two identity principals exist independent of any household, so they are provisioned on
        # the unscoped connection here (ticket 0046, KTD-10). Idempotent, so every archetype's seed
        # run converges on the same two rows.
        _provision_demo_users(conn)

    with repository(engine, household_id) as repo:
        # The memberships that make this synthetic household reachable — the demo `viewer` and the
        # reviewer `owner`. Written through the scoped repository so RLS `WITH CHECK` binds them to
        # this household, exactly like every other scoped write.
        _seed_memberships(repo)

        store = PostgresSnapshotStore(repo.conn)

        sweeps = refusals = 0
        final: Any = None

        for w in walk(history, spec, start, total_days):
            if w.offset < warmup_days:
                # The engine genuinely refuses below 60 days of history. Those refusals are real,
                # they are simply not the part of the story worth serving — same runway `build()`
                # walks and discards.
                continue

            ref = store.put(household_id, w.day, w.snapshot)
            _write_decision(repo, w, ref)

            if w.decision.action is Action.SWEEP:
                sweeps += 1
            else:
                refusals += 1
            final = w

        if final is None:
            raise SeedError(f"{archetype}: walked {total_days} days and served none")

        # Current state, as of the last day served. Written after the decisions rather than before
        # because that is the order the facts arrive in: the snapshot is what the engine saw, and
        # these rows are what it saw *last*.
        _write_current_state(repo, final)

        # The spend surface's History-derived half. **This is the only thing the seeder knows that
        # the database cannot be asked** — the rolling 30-day series and each card's last cycle come
        # from every transaction, and there is no `transactions` table until ingest lands. So it is
        # derived here, from the history this run walked, and written in the same transaction as the
        # decisions it describes. Ticket 0031; `backend/spend.py` carries the reasoning.
        _write_spend_projection(repo, history, final)

    return Seeded(
        household_id=household_id,
        archetype=archetype,
        days=sweeps + refusals,
        sweeps=sweeps,
        refusals=refusals,
    )


def _reset_household(conn: Connection, household_id: str, archetype: str) -> None:
    # ON DELETE CASCADE carries accounts, cards, policies and snapshots. `decisions` is partitioned
    # and has no FK to households (a partitioned table cannot be the referencing side of one here),
    # so it is deleted explicitly — forgetting it would leave a previous run's decisions attached to
    # a household that had been rewritten underneath them.
    conn.execute(text("DELETE FROM decisions WHERE household_id = :h"), {"h": household_id})
    # DELETE cascades to household_members (FK ON DELETE CASCADE), so re-seeding converges on the
    # memberships too rather than accumulating stale ones.
    conn.execute(text("DELETE FROM households WHERE id = :h"), {"h": household_id})
    # `is_demo = true`: every seeded household is synthetic and belongs to the public demo plane
    # (ticket 0046, KTD-10). A real household is created elsewhere and is never a demo one.
    conn.execute(
        text("INSERT INTO households (id, archetype, is_demo) VALUES (:h, :a, true)"),
        {"h": household_id, "a": archetype},
    )


def _provision_demo_users(conn: Connection) -> None:
    """Provision the demo `viewer` and reviewer `owner` users, idempotently (ticket 0046, KTD-10).

    On the unscoped connection because `users` is platform-level (not HOUSEHOLD_SCOPED). Through
    `add_user`, which is `ON CONFLICT (stytch_user_id) DO NOTHING`, so calling this once per
    archetype converges on exactly two rows.
    """
    from backend.db.repository import add_user

    add_user(conn, user_id=DEMO_USER_ID, stytch_user_id=DEMO_USER_STYTCH_ID, email=DEMO_USER_EMAIL)
    # The demo viewer is on the demo plane, and the row says so (ticket 0058) — `add_user` defaults
    # `is_demo` false and is DO NOTHING on re-run, so the flag is asserted here rather than assumed.
    # Without it the seeded viewer could `POST /households` and become owner of a real one.
    conn.execute(text("UPDATE users SET is_demo = true WHERE id = :u"), {"u": DEMO_USER_ID})
    add_user(
        conn,
        user_id=REVIEWER_USER_ID,
        stytch_user_id=REVIEWER_USER_STYTCH_ID,
        email=REVIEWER_USER_EMAIL,
    )


def _seed_memberships(repo: Repository) -> None:
    """Make this household reachable by the demo `viewer` and the reviewer `owner` (ticket 0046).

    Idempotent (`add_membership` upserts the role), so a re-seed converges. The demo user is a
    `viewer` — read-only, the public principal — and the reviewer is an `owner` who may write.
    """
    repo.add_membership(user_id=DEMO_USER_ID, role="viewer")
    repo.add_membership(user_id=REVIEWER_USER_ID, role="owner")


def _write_decision(repo: Repository, w: Any, snapshot_ref: str) -> None:
    d = w.decision
    repo.add_decision(
        decision_id=f"{repo.household_id}:{w.day.isoformat()}",
        day=w.day,
        action=d.action.value,
        amount=d.amount,
        target_card_id=d.target_debt_id,
        # None on a blocking refusal, and the column is nullable for exactly that reason: the
        # forecast never ran, so there is no projection. A zero here would look like a perfect one.
        projected_low_balance=d.projected_low_balance,
        reasons=_encode_reasons(d.reasons),
        engine_version=ENGINE_VERSION,
        snapshot_ref=snapshot_ref,
    )


def _encode_reasons(reasons: Any) -> str:
    """Reason codes and their params, as queryable JSON.

    `reasons` is JSONB and not opaque — unlike the snapshot payload, refusal-rate metrics slice it
    (`prd.md` §5.3). Params go through `codec.encode_scalar` because they carry `Decimal` money and
    `date`s, and JSON has neither: a cent that round-trips through a float is not the cent the
    engine decided on.

    Serialized with `json.dumps` rather than handed over as a `list`, matching
    `snapshots.PostgresSnapshotStore.put` — psycopg will not adapt a Python container to JSONB
    through a textual bind, and Postgres casts the string on the way in.
    """
    from backend.codec import encode_scalar

    return json.dumps(
        [
            {
                "code": r.code.value,
                "params": {k: encode_scalar(v) for k, v in (r.params or {}).items()},
            }
            for r in reasons
        ]
    )


def _write_spend_projection(repo: Repository, history: History, w: Any) -> None:
    """Derive and store the projection for the last day served. Ticket 0031.

    **`w.day` and nothing else.** The projection describes one day, and it must be the same day the
    obligations are read from — `backend/spend.py:assemble` refuses to serve a projection whose
    `as_of` disagrees with the snapshot beside it, rather than render this month's statement next to
    last month's spending with no hint that it had.

    Derived from `history` rather than from the walk's own accumulated state on purpose: this is the
    same `derive_spend_projection` the artifact pipeline calls, so the seeded surface and the
    committed fixture cannot drift. That is `0019`'s rule applied one function further out.
    """
    projection = derive_spend_projection(history, w.day, w.snapshot.portfolio)

    repo.set_spend_projection(
        as_of=projection.as_of,
        # `encode_tree`, not `json.dumps`: the payload carries `Decimal` money and `date`s, and JSON
        # has neither. A cent that round-trips through a float is not the cent the engine decided
        # on. Same codec as the snapshot payload, for the same reason.
        payload=json.dumps(encode_tree(projection)),
    )


def _write_current_state(repo: Repository, w: Any) -> None:
    snap = w.snapshot

    for acct in snap.accounts:
        repo.add_account(
            account_id=acct.account_id,
            kind=acct.kind.value,
            balance=acct.balance,
            connection=acct.connection.value,
            balance_age_days=acct.balance_age_days,
        )

    for card in snap.portfolio.cards:
        repo.add_card(
            card_id=card.card_id,
            apr=card.apr,
            # Ticket 0028's whole point, and it must be *written*: a 23% estimate and a reported
            # 23% are the same number, and only this tells them apart. Omitting it stored
            # `reported` from the column default — see `Repository.add_card`.
            apr_source=card.apr_source.value,
            close_day_of_month=card.cycle.close_day_of_month,
            grace_days=card.cycle.grace_days,
            statement_balance=card.statement_balance,
            statement_due_date=card.statement_due_date,
            minimum_payment=card.minimum_payment,
            unbilled_balance=card.unbilled_balance,
            next_close_date=card.next_close_date,
            behavior=card.behavior.value,
            observed_monthly_payment=card.observed_monthly_payment,
            observed_monthly_charges=card.observed_monthly_charges,
        )

    p = snap.policy
    repo.set_policy(
        buffer_floor=p.buffer_floor,
        max_sweep=p.max_sweep,
        max_weekly_sweep=p.max_weekly_sweep,
        min_days_between_sweeps=p.min_days_between_sweeps,
        blackout_dates=[d.isoformat() for d in p.blackout_dates],
    )


def seed_all(engine: Engine) -> list[Seeded]:
    """Every archetype. The dashboard's population."""
    from backend.archetypes import ARCHETYPES

    return [seed(engine, name, spec) for name, spec in ARCHETYPES.items()]

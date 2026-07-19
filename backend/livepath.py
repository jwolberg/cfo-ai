"""The live-assembly path — a decision re-decided from a household's *current* policy and
attestation. Ticket 0056.

The write paths (`0049` settings, `0050` attestation) persist, audit, and read back a user's
guardrails and card attestation — but nothing fed them into a live `decide()`. The demo
`assemble_snapshot()` takes a hardcoded `UserPolicy` and the `attested` bool defaults `True`; the
walk/replay/seeder pass those defaults on purpose, so the regression oracle keeps grading the engine
that shipped. And `backend/readpath.py` serves **frozen** precomputed snapshots. So a user could
lower their buffer floor or attest their cards and `Repository.policy()` / `attested_for` would
return the new values, while the decision the app showed was computed from the old ones.

This closes that gap **without touching the oracle**. `assemble_snapshot` stays pure over its
inputs; the live values are *passed in*, as `sweeps_in_flight` and `attested` already are. The one
canonical `walk()` grows a single `attested` parameter (default `True`, so `build()`/`replay()`/the
seeder are unchanged) and this path drives it with `policy=Repository.policy()` and
`attested=attested_for(repo)`. A write now changes the next decision.

**Whose history?** A live household is a *linked* one, and the `plaid_transactions` → `History`
adapter is the next rung's work — `backend/plaid/__init__.py` says ingest "stops one seam short of
`assemble_snapshot()`". So `live_decision()` takes the `History` as an input (the linked rung will
supply it), and `linked_history()` — the route's source for it — is the honest stub that raises
`NoLinkedHistory` until that adapter lands. The seam is built and proven now; its first real
consumer arrives with the Link rung.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from backend.attestation import attested_for
from backend.db.repository import Repository
from backend.precompute import WalkDay, walk
from engine.models import UserPolicy
from sim.household import History

if TYPE_CHECKING:
    from backend.spend import SpendSurface


class NoLivePolicy(LookupError):
    """The household has no `policy_events` to assemble from. Every seeded household has one
    (the seeder writes an initial event), so this is a real fault — a 404, not a 500."""


class NoLinkedHistory(LookupError):
    """No transaction `History` exists for this household yet.

    The live path needs the household's realized history to assemble a snapshot, and the
    `plaid_transactions` → `History` adapter is the deferred Link rung's work. Raised by
    `linked_history()` until that seam lands; the route renders it as "linked, but no data yet"
    rather than a decision.
    """


def user_policy(row: dict[str, object]) -> UserPolicy:
    """Reconstruct a `UserPolicy` from a `Repository.policy()` row.

    The inverse of `main._policy_json`, but into the engine's dataclass rather than the wire shape:
    money stays `Decimal` (the DB gives it back as `Numeric`), `blackout_dates` becomes the
    `frozenset[date]` the engine holds. `UserPolicy.__post_init__` re-validates spacing on the way
    in, so a corrupt row raises here rather than deciding on nonsense.
    """
    return UserPolicy(
        buffer_floor=row["buffer_floor"],  # type: ignore[arg-type]
        max_sweep=row["max_sweep"],  # type: ignore[arg-type]
        max_weekly_sweep=row["max_weekly_sweep"],  # type: ignore[arg-type]
        blackout_dates=frozenset(row["blackout_dates"]),  # type: ignore[arg-type]
        min_days_between_sweeps=row["min_days_between_sweeps"],  # type: ignore[arg-type]
    )


def live_decision(repo: Repository, history: History, today: date) -> WalkDay:
    """Re-decide `today` for a household from its **current** policy and attestation.

    Reads `Repository.policy()` → `UserPolicy` and `attested_for(repo)` → `bool`, then walks the
    household's `history` with those live values and returns the `today` day — decision and the
    snapshot it saw. Because the values are read here and passed *into* the pure `walk()` /
    `assemble_snapshot()`, a settings or attestation write changes the next decision while the
    replay oracle keeps grading the shipped engine (it still takes the defaults).

    `spec` is `history.spec` — the history already carries the household it belongs to; there is no
    second copy to disagree. `today` must fall within the history's window.
    """
    policy_row = repo.policy()
    if policy_row is None:
        raise NoLivePolicy(repo.household_id)

    if not history.start <= today <= history.end:
        raise ValueError(
            f"today={today} is outside the history window {history.start}..{history.end}"
        )

    policy = user_policy(policy_row)
    attested = attested_for(repo)
    days = (today - history.start).days + 1

    result: WalkDay | None = None
    for day in walk(history, history.spec, history.start, days, policy=policy, attested=attested):
        result = day
    # The walk yields one day per step up to `today`, so the last is `today`. A None here would
    # mean `days < 1`, which the window guard above already forbids.
    assert result is not None and result.day == today
    return result


def live_spend(repo: Repository, history: History) -> SpendSurface:
    """The live **spend surface** for a linked household — the comprehension half of the Link rung.

    The seeded `/spend` path (`readpath.load_spend_surface`) reads a persisted `decisions` row and a
    seeded `spend_projection`, which a linked household has neither of. This builds the same surface
    from live data instead, reusing exactly what the seeder does (`backend/seed.py`): walk the
    linked `History` to today's snapshot, `derive_spend_projection(history, today,
    snapshot.portfolio)`, then `assemble(snapshot, projection)`. The projection's `as_of` and the
    snapshot's `today` are the same day by construction (both `history.end`), so `assemble`'s
    staleness guard is satisfied.

    Takes `history` as an input, as `live_decision` does — the route builds it once via
    `linked_history(repo)` and threads it in. Raises `NoLivePolicy` (no guardrails set) through
    `live_decision`. Local imports keep the `livepath`↔`precompute`/`spend` dependency
    one-directional, as `linked_history` does.
    """
    from backend.precompute import derive_spend_projection
    from backend.spend import assemble

    day = live_decision(repo, history, history.end)
    projection = derive_spend_projection(history, day.day, day.snapshot.portfolio)
    return assemble(day.snapshot, projection)


def linked_history(repo: Repository) -> History:
    """The realized `History` for a linked household — the live path's data source.

    Delegates to `backend/linkedpath.py`, the adapter that turns the household's ingested Plaid data
    (`plaid_transactions` + the 0014 balance/liability snapshots + `backend/recurring.py`) into a
    `History`. It raises `NoLinkedHistory` when the data cannot support a decision — too little
    history, no depository account, no card, no detectable income — the same signal this seam has
    always raised, so `live_decision()` and the route need no change. The import is local, to keep
    `livepath`↔`linkedpath` dependency one-directional (the adapter imports `NoLinkedHistory` here).
    """
    from backend.linkedpath import build_linked_history

    return build_linked_history(repo)

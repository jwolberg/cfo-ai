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

from backend.attestation import attested_for
from backend.db.repository import Repository
from backend.precompute import WalkDay, walk
from engine.models import UserPolicy
from sim.household import History


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


def linked_history(repo: Repository) -> History:
    """The realized `History` for a linked household — the live path's data source.

    The `plaid_transactions` → `History` adapter is the deferred Link rung (see the module
    docstring). Until it lands there is nothing to build a live decision from, so this raises
    `NoLinkedHistory` rather than fabricating one. Kept as its own seam so the day the adapter
    arrives, only this function changes and `live_decision()` already consumes it.
    """
    raise NoLinkedHistory(repo.household_id)

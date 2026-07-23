"""The decision, opened up: every gate `decide()` evaluated, in the order it ran them.

`engine/decide.py` is the whole product, and it is deliberately terse — it returns a `Decision`,
not a story about how it got there. For a regulator, an operator, or a CEO asking "why *that*
number", the story is the point. This module reconstructs it **without re-deciding**: it walks the
exact same gate sequence `decide()` walks, calling the exact same helpers on the same `Snapshot`,
and records what each one read, what it was measured against, and whether it stopped the decision.

## Why this cannot drift into a flattering lie

An audit trail that is a *second implementation* of the decision is worse than none — it can agree
with the engine on the happy path and diverge exactly where it matters. Two things hold it honest:

1. **It reuses the engine's own helpers** (`_blocking_reasons`, `conservative_low_balance`,
   `untouchable`, `_cadence_hold`, `apply_caps`, `claimable_interest_avoided`). No gate's *value* is
   recomputed here; only the *order* is mirrored.
2. **The headline it reports is `decide()`'s own output**, not a reconstruction. `trace()` calls
   `decide(snapshot)` and reports that action/amount/target verbatim. The steps explain that
   decision; they do not stand in for it.

`tests/test_trace.py` pins the mirror to the engine across the whole demo window: for every day, the
terminal gate the trace lands on must match the action `decide()` actually returned. If someone
reorders `decide()` and forgets this file, that test fails — which is the only acceptable way for an
audit trail to notice it has gone stale.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from engine.decide import (
    MAX_BALANCE_AGE_DAYS,
    MAX_INCOME_VARIATION,
    MIN_HISTORY_DAYS,
    MIN_IDLE_TO_MENTION,
    MIN_SWEEP,
    _blocking_reasons,
    _cadence_hold,
    _coverage_reasons,
    _idle_elsewhere,
    _select_target,
    apply_caps,
    decide,
    obligation_in_horizon,
    untouchable,
)
from engine.forecast import HORIZON_DAYS, conservative_low_balance
from engine.interest import claimable_interest_avoided
from engine.models import Snapshot

# A gate's verdict, for rendering. `PASS` = the gate was satisfied and the decision continued.
# `BLOCK` = this gate stopped the decision (it is the reason for a refusal). `INFO` = the step
# computed a value or attached a note but is not itself a yes/no gate (the forecast, the surplus
# arithmetic, the interest claim).
PASS = "pass"
BLOCK = "block"
INFO = "info"


@dataclass(frozen=True)
class GateStep:
    """One rung of `decide()`, with the inputs it read and the threshold it read them against."""

    order: int
    stage: str
    name: str
    question: str
    inputs: dict[str, str]
    threshold: str | None
    result: str
    detail: str
    terminal: bool


@dataclass(frozen=True)
class DecisionTrace:
    """The full path for one day: the engine's real decision, and the gates that produced it."""

    day: date
    action: str
    amount: Decimal
    target: str | None
    steps: tuple[GateStep, ...]


def _f(d: Decimal) -> str:
    """A dollar figure the way the panel shows it."""
    return f"${d:,.2f}"


def trace(snapshot: Snapshot) -> DecisionTrace:
    """Replay `decide()`'s gate sequence over `snapshot`, recording each rung.

    Mirrors `engine.decide.decide` step for step. The returned action/amount/target come from
    `decide()` itself, so the headline can never disagree with the engine.
    """
    s = snapshot
    steps: list[GateStep] = []
    order = 0

    def add(stage, name, question, inputs, threshold, result, detail, terminal=False):
        nonlocal order
        order += 1
        steps.append(
            GateStep(
                order=order,
                stage=stage,
                name=name,
                question=question,
                inputs=inputs,
                threshold=threshold,
                result=result,
                detail=detail,
                terminal=terminal,
            )
        )

    real = decide(s)

    # --- 1. Preconditions (engine.decide._blocking_reasons) ---
    # Seven checks that must all hold before the forecast is even worth running. Any one short-
    # circuits to a refusal. We surface them as one rung with the individual inputs, since on the
    # demo household they all pass and the interesting failures live elsewhere.
    funding = next((a for a in s.accounts if a.account_id == s.funding_account_id), None)
    blocking = _blocking_reasons(s)
    add(
        "Preconditions",
        "Can we act at all?",
        "Funding account healthy and fresh, enough history, stable income, not in a blackout, no "
        "sweep already in flight.",
        {
            "days since checking balance updated": str(funding.balance_age_days)
            if funding
            else "—",
            "bank connection status": funding.connection.value if funding else "—",
            "days of transaction history": str(s.history_days),
            "month-to-month income variation": f"{s.income_variation:.2%}",
            "sweep already in flight (amount)": _f(s.sweeps_in_flight),
        },
        f"age ≤ {MAX_BALANCE_AGE_DAYS}d · history ≥ {MIN_HISTORY_DAYS}d · "
        f"income variation ≤ {MAX_INCOME_VARIATION:.0%}",
        BLOCK if blocking else PASS,
        (
            "Blocked: " + ", ".join(r.code.value for r in blocking)
            if blocking
            else "All preconditions met — proceed to the portfolio."
        ),
        terminal=bool(blocking),
    )
    if blocking:
        return DecisionTrace(
            s.today, real.action.value, real.amount, real.target_debt_id, tuple(steps)
        )

    # --- 2. Coverage (engine.decide._coverage_reasons) ---
    # Before any surplus is computed: if we cannot see the whole card portfolio, no amount of
    # visible surplus justifies moving money into part of it.
    coverage = _coverage_reasons(s)
    add(
        "Coverage",
        "Can we see the whole portfolio?",
        "Every card is visible and its payment behaviour is known — otherwise a sweep "
        "could pay the wrong card, or one we cannot see.",
        {
            "coverage": s.portfolio.coverage.value,
            "cards": str(len(s.portfolio.cards)),
        },
        "coverage = complete · no card with unknown behaviour",
        BLOCK if coverage else PASS,
        (
            "Blocked: " + ", ".join(r.code.value for r in coverage)
            if coverage
            else "Portfolio fully visible — proceed to the forecast."
        ),
        terminal=bool(coverage),
    )
    if coverage:
        return DecisionTrace(
            s.today, real.action.value, real.amount, real.target_debt_id, tuple(steps)
        )

    # --- 3. Forecast (engine.forecast.conservative_low_balance) ---
    low, low_day = conservative_low_balance(s)
    add(
        "Forecast",
        "How low does cash get?",
        "The lowest the checking balance is projected to reach over the horizon — the "
        "floor we must protect before moving anything.",
        {
            "checking now": _f(funding.balance) if funding else "—",
            "projected low": _f(low),
            "on": low_day.isoformat(),
        },
        None,
        INFO,
        f"Cash bottoms out at {_f(low)} on {low_day.isoformat()}.",
    )

    # --- 4. Target selection (engine.decide._select_target) ---
    target, cannot_rank = _select_target(s.portfolio)
    add(
        "Target",
        "Which card would we pay?",
        "The highest-APR card it is honest to sweep to (never a transactor — their grace period "
        "already does what a sweep claims to).",
        {
            "target": target.card_id if target else "—",
            "APR": f"{target.apr:.2%}" if target and target.apr is not None else "—",
        },
        "an open, interest-bearing card with a known APR",
        BLOCK if cannot_rank else PASS,
        (
            f"Blocked: {cannot_rank.code.value}"
            if cannot_rank
            else f"Target is {target.card_id} at {target.apr:.2%} APR."
        ),
        terminal=bool(cannot_rank),
    )
    if cannot_rank:
        return DecisionTrace(
            s.today, real.action.value, real.amount, real.target_debt_id, tuple(steps)
        )

    # --- 5. Untouchable + surplus arithmetic (engine.decide.untouchable) ---
    # The reserve is a sum of per-card obligations (`obligation_in_horizon`); itemise it so the
    # subtraction is auditable card by card rather than as one opaque total. Ordered dict → the
    # renderers lay it out as a vertical ledger: projected low, then the buffer and each card
    # subtracted, then the available result.
    buffer_floor, reserved = untouchable(s)
    available = low - buffer_floor - reserved
    horizon_end = s.today + timedelta(days=HORIZON_DAYS)
    surplus_inputs = {"projected low": _f(low), "− buffer floor": _f(buffer_floor)}
    reserved_cards = 0
    for card in s.portfolio.cards:
        obligation = obligation_in_horizon(card, horizon_end)
        if obligation > 0:
            surplus_inputs[f"− reserve · {card.card_id}"] = _f(obligation)
            reserved_cards += 1
    surplus_inputs["= available"] = _f(available)
    cards_phrase = (
        f"{_f(reserved)} reserved across {reserved_cards} card{'s' if reserved_cards != 1 else ''}"
        if reserved_cards
        else "nothing reserved for cards"
    )
    add(
        "Surplus",
        "Is there money that is ours to move?",
        "What is left of the projected low after the safety buffer and the cash already "
        "reserved for each card's obligation coming due in the horizon.",
        surplus_inputs,
        None,
        INFO,
        f"After the {_f(buffer_floor)} buffer and {cards_phrase}, {_f(available)} is available.",
    )

    # --- 6. Cadence hold (engine.decide._cadence_hold) — AFTER the forecast, deliberately ---
    # This is the gate that makes the demo: it can hold a day that has real surplus, so a refusal
    # here is "we could, but the cadence says wait", not "there was nothing to move".
    hold = _cadence_hold(s)
    add(
        "Cadence",
        "Have we swept too recently?",
        "A minimum gap between sweeps, so the household is not drained in a cluster. "
        "Evaluated even when surplus exists — this is a rate limit, not a money check.",
        {
            "days since last sweep": (
                str(s.days_since_last_sweep) if s.days_since_last_sweep is not None else "never"
            ),
            "surplus available": _f(available),
        },
        f"days since last sweep ≥ {s.policy.min_days_between_sweeps}",
        BLOCK if hold else PASS,
        (
            f"Held: last sweep was {s.days_since_last_sweep} days ago, under the "
            f"{s.policy.min_days_between_sweeps}-day gap — {_f(available)} of surplus stays put."
            if hold
            else f"Clear: {s.days_since_last_sweep} days since the last sweep meets the "
            f"{s.policy.min_days_between_sweeps}-day gap."
        ),
        terminal=bool(hold),
    )
    if hold:
        return DecisionTrace(
            s.today, real.action.value, real.amount, real.target_debt_id, tuple(steps)
        )

    # --- 7. Surplus threshold ---
    below_surplus = available < MIN_SWEEP
    add(
        "Surplus check",
        "Is the surplus worth moving?",
        "The available surplus must clear the minimum sweep to be worth an ACH at all.",
        {"available": _f(available)},
        f"available ≥ {_f(MIN_SWEEP)}",
        BLOCK if below_surplus else PASS,
        (
            f"Blocked: {_f(available)} is below the {_f(MIN_SWEEP)} minimum (no_surplus)."
            if below_surplus
            else f"{_f(available)} clears the {_f(MIN_SWEEP)} minimum."
        ),
        terminal=below_surplus,
    )
    if below_surplus:
        return DecisionTrace(
            s.today, real.action.value, real.amount, real.target_debt_id, tuple(steps)
        )

    # --- 8. Caps (engine.decide.apply_caps) ---
    amount, cap_reasons = apply_caps(s, target, available)
    add(
        "Caps",
        "How much, after the limits?",
        "The surplus, trimmed by the per-sweep cap, the weekly cap, and never more than the card "
        "owes.",
        {
            "available": _f(available),
            "per-sweep cap": _f(s.policy.max_sweep),
            "weekly cap": _f(s.policy.max_weekly_sweep),
            "card owes": _f(target.total_owed),
            "= sweep": _f(amount),
        },
        None,
        BLOCK if amount < MIN_SWEEP else INFO,
        (
            "; ".join(r.code.value for r in cap_reasons) + f" → {_f(amount)}"
            if cap_reasons
            else f"No cap binds — full {_f(amount)} moves."
        ),
        terminal=amount < MIN_SWEEP,
    )
    if amount < MIN_SWEEP:
        return DecisionTrace(
            s.today, real.action.value, real.amount, real.target_debt_id, tuple(steps)
        )

    # --- 9. Interest avoided (engine.interest.claimable_interest_avoided) ---
    saved = claimable_interest_avoided(target, amount, s.today)
    add(
        "Interest",
        "What does the sweep save?",
        "The interest this payment avoids, against the household's own observed payment behaviour "
        "held constant. None is claimed when the APR is unknown or the card does not amortise.",
        {"sweep": _f(amount), "APR": f"{target.apr:.2%}" if target.apr is not None else "—"},
        None,
        INFO,
        f"About {_f(saved)} of interest avoided." if saved is not None else "No claim made.",
    )

    # --- Outcome ---
    idle = _idle_elsewhere(s)
    add(
        "Decision",
        "Sweep.",
        "Move the surplus to the target card.",
        {
            "action": real.action.value,
            "amount": _f(real.amount),
            "target": real.target_debt_id or "—",
            "idle cash noted": _f(idle[0].params["amount"]) if idle else "—",
        },
        f"idle mentioned above {_f(MIN_IDLE_TO_MENTION)}",
        INFO,
        f"SWEEP {_f(real.amount)} to {real.target_debt_id}.",
        terminal=True,
    )

    return DecisionTrace(s.today, real.action.value, real.amount, real.target_debt_id, tuple(steps))

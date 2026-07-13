"""Grading a decision against what actually happened.

`decide()` emits a prediction. Until this module existed, nothing in the repo ever settled the
bet — the engine's central claim ("this $220 is not needed") had never once been checked
against the future that followed it.

[`strategy.md`](../docs/strategy.md) §3 says the only asset that compounds is **calibration**:
"the empirical distribution of our own errors, by user archetype, by pay cadence, by season."
This module is that distribution's data structure. It is what makes the engine falsifiable, and
therefore what makes every later improvement to it defensible rather than merely plausible.

Every field is a metric the PRD or the strategy names by hand. None of them is invented here.

## The signature is the safety property

`grade()` does **not** take "the realized balances". It takes `Realized` — the household's own
daily cash movement, *excluding anything we did* — and applies the decision's sweep **itself**.

This is deliberate and it is the whole design. A replay that grades each day's decision against
the household's untouched history never compounds the effect of its own sweeps, and will
systematically **understate** breach risk: it reports a tail that would only have been real if
we had never acted. That is the difference between a shadow-mode report and a shadow-mode lie,
and it is not the sort of thing to leave to a caller's diligence. So the caller cannot forget to
apply the sweep, because the caller is not the one who applies it.

## We assume our money leaves immediately

`settles_after_days` defaults to **0**: the sweep is charged against the funding account on the
day we decided, not when ACH would plausibly have posted it. That is the assumption most likely
to *find* a breach rather than excuse one. A grader that flatters itself is worse than no
grader, because it converts an unknown risk into a false sense of a known one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from engine.decide import untouchable, would_sweep
from engine.forecast import HORIZON_DAYS
from engine.models import ZERO, Action, Decision, ReasonCode, Snapshot, money


def sum_reserved(snapshot: Snapshot) -> Decimal:
    return untouchable(snapshot)[1]


@dataclass(frozen=True)
class Realized:
    """What the household actually did — **excluding anything we did.**

    `net_by_day` is the funding account's net cash movement on each day of the horizon — their
    pay, their bills, their spending — for the days **after** today (today's movements are
    already inside the snapshot's balance). Our sweep is *not* in here, and must not be.
    `grade()` applies it.

    In shadow mode this is simply the household's real history (we moved nothing). In live mode
    it is their history with our own settled sweeps added back — the same canonical form either
    way, so a decision graded in shadow and a decision graded in production mean the same thing.
    """

    net_by_day: Mapping[date, Decimal]


@dataclass(frozen=True)
class Outcome:
    """One decision, graded. The atom of the calibration asset."""

    action: Action
    swept: Decimal

    # --- what we said would happen, and what did -------------------------------------
    projected_low: Decimal
    realized_low: Decimal  # the path our sweep actually produced
    realized_low_unswept: Decimal  # the path the household would have had without us

    # Signed: `realized_low_unswept - projected_low`.
    #   positive -> we were conservative; the money was there after all.
    #   negative -> we were OPTIMISTIC. This is the tail that decides the company.
    #
    # Measured against the household's **unswept** path, and that is not a detail.
    # `Decision.projected_low_balance` is a projection of the household's *pre-sweep*
    # trajectory — the sweep is decided *from* it, not included *in* it. Comparing it against
    # the post-sweep low would fold the entire size of our own sweep into the "error", making
    # the engine look wildly optimistic precisely on the days it swept hardest and was right.
    # That would poison the one distribution strategy.md §3 calls the only asset that compounds.
    #
    # The sign convention is load-bearing forever; a flip here silently inverts the dial every
    # future calibration decision is read off.
    projection_error: Decimal

    # --- prd.md §5.2, the guardrail that outranks the primary KPI ---------------------
    overdrafted: bool  # they went below zero, for any reason
    # They went below zero AND would not have without us. §5.2 says "sweep-caused" and means
    # it: a household that overdrafts on its own is not our doing, and folding those in would
    # make the guardrail metric meaningless — it would be dominated by lives we never touched.
    sweep_caused_overdraft: bool
    breached_buffer: bool  # below the floor but still solvent — the buffer did its job

    # --- prd.md §5.3, the cost of conservatism ---------------------------------------
    # The raw surplus that was genuinely there, ignoring every guardrail.
    hindsight_safe: Decimal
    # What decide() would have moved with a perfect forecast, obeying the user's own caps.
    should_have_swept: Decimal
    # `should_have_swept - swept`. **Our** forecast error, priced. A cap the user chose is not
    # our conservatism, so it is excluded — see engine.decide.apply_caps.
    false_refusal_cost: Decimal

    # What we told the user this bought them. Whether it was *realized* depends on their
    # payments over the following years and belongs to the KPI layer, not to a 30-day grade.
    interest_claimed: Decimal | None


def _low_balance(opening: Decimal, realized: Realized, today: date, sweep: Decimal, settles: int):
    """Walk the horizon, applying our sweep on the day it leaves, and take the running low.

    `opening` is the funding account's balance at the end of **today** — so today's own
    movements are already in it, and the ledger covers the days *after* today. Applying day
    zero again would double-count it, and a grader that double-counts is a grader that invents
    breaches.

    The low, not the ending balance — the user only has to be broke once.
    """
    balance = opening
    sweep_day = today + timedelta(days=settles)

    if sweep_day <= today:
        balance -= sweep

    low = balance
    for offset in range(1, HORIZON_DAYS + 1):
        day = today + timedelta(days=offset)

        if day == sweep_day:
            balance -= sweep

        balance += realized.net_by_day[day]

        if balance < low:
            low = balance

    return low


def grade(
    snapshot: Snapshot,
    decision: Decision,
    realized: Realized,
    settles_after_days: int = 0,
) -> Outcome:
    """Settle the bet. See the module docstring for why `realized` excludes our own sweep."""
    if decision.projected_low_balance is None:
        raise ValueError(
            "decision has no projection to grade against — a blocking refusal never computed "
            "a low balance, and inventing one would corrupt the calibration distribution with "
            "zeros that look like perfect forecasts"
        )

    horizon = [snapshot.today + timedelta(days=i) for i in range(1, HORIZON_DAYS + 1)]
    if missing := [d for d in horizon if d not in realized.net_by_day]:
        raise ValueError(
            f"realized ledger does not cover the {HORIZON_DAYS}-day horizon "
            f"({len(missing)} days missing, first {missing[0]}). Grading against a partial "
            "future would silently flatter the projection."
        )

    account = next(a for a in snapshot.accounts if a.account_id == snapshot.funding_account_id)

    unswept = _low_balance(account.balance, realized, snapshot.today, ZERO, settles_after_days)
    low = _low_balance(
        account.balance, realized, snapshot.today, decision.amount, settles_after_days
    )

    buffer_floor, _ = untouchable(snapshot)

    # What was genuinely there to take, ignoring every guardrail — the raw hindsight surplus.
    hindsight_safe = max(ZERO, unswept - buffer_floor - sum_reserved(snapshot))

    # What `decide()` would have moved with a perfect forecast — obeying the user's caps, the
    # weekly limit, and the card balance, exactly as it does in production. This is the honest
    # counterfactual, and it is computed by decide() itself rather than re-derived here: a
    # second copy of the cap ladder would drift, and the day it drifted the calibration numbers
    # would start describing an engine that never shipped.
    should_have_swept = would_sweep(snapshot, unswept)

    # The cost of *our* conservatism (prd.md §5.3) — and only ours. If the user capped us at
    # $300 and we moved $300, we were not being conservative, we were being obedient, and this
    # is zero however much idle cash hindsight reveals.
    #
    # It fires on an **under-sweep**, not only on an outright refusal. That matters enormously:
    # the spend-model bug in decision-engine.md [6.5] does not refuse, it under-sweeps. A metric
    # that only counted refusals would have been blind to the largest error in the engine.
    false_refusal_cost = max(ZERO, should_have_swept - decision.amount)

    claimed = next(
        (r.params["amount"] for r in decision.reasons if r.code is ReasonCode.INTEREST_AVOIDED),
        None,
    )

    return Outcome(
        action=decision.action,
        swept=decision.amount,
        projected_low=decision.projected_low_balance,
        realized_low=low,
        realized_low_unswept=unswept,
        projection_error=money(unswept - decision.projected_low_balance),
        overdrafted=low < ZERO,
        sweep_caused_overdraft=low < ZERO <= unswept,
        breached_buffer=low < buffer_floor,
        hindsight_safe=hindsight_safe,
        should_have_swept=should_have_swept,
        false_refusal_cost=false_refusal_cost,
        interest_claimed=claimed,  # type: ignore[arg-type]
    )

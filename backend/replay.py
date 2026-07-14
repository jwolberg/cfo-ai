"""Grade every decision the engine would have made, against what the household actually did.

`engine/outcome.py` has existed since 2026-07-13 and **nothing has ever called it**. This is the
caller. Until it exists, the engine has no measured error distribution — and
`docs/learnings/2026-07-13-the-spend-model-over-reserves.md` is unambiguous about the consequence:

    "Do not fix it yet. Land the grader (#3) and the replay driver (#4) first. Ship the new spend
    model with its dial set to reproduce today's refusals — no behaviour change, no new risk.
    Loosen it only as far as the **measured** breach rate licenses."

The forecast is known to over-reserve by **$400–970**. Fixing that *loosens* the reserve, which is
the one direction `decision-engine.md` §3 forbids without evidence. This module is the evidence.

**It changes no decision.** It observes.

## Two ways a replay lies, both of them flattering

**1. Grading against the household's untouched history.** `grade()` deliberately takes `Realized` —
the household's own cash movement, *excluding anything we did* — and applies the sweep itself. A
replay that instead grades each decision against the untouched path never compounds the effect of
its own sweeps, and so systematically **understates** breach risk: it reports the tail we would have
had if we had never acted. That is the difference between a shadow-mode report and a shadow-mode
lie, and it is far too load-bearing to leave to a caller's diligence — which is exactly why the
caller is not the one who applies the sweep.

**2. Counting a deferral as a loss.** `false_refusal_cost` is "money we left idle that was genuinely
safe to move" — our cost of conservatism (prd.md §5.3). But on a `CADENCE_HOLD` day that money is
not *lost*, it moves next week. Total it naively and the same dollars are counted again every day
they sit, and the metric that is supposed to measure our forecast error instead measures how long a
cadence rule made someone wait.

The same question arrives with this feature's new blocking codes, and it has the same answer:

- `CARD_COVERAGE_INCOMPLETE` — a **deferral**. It resolves the moment they attest or connect the
  card we can see a payment to.
- `CARD_BEHAVIOR_UNKNOWN` — a **deferral**. It resolves by itself after three observed cycles.

Neither is a permanent cost of conservatism, and folding them into `false_refusal_cost` would price
a refusal we were *right* to make as a failure — teaching the calibration dial to talk us out of the
safety gate this feature exists to add.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from backend.precompute import (
    DEMO_POLICY,
    DEMO_SPEC,
    SEED,
    SERVED_DAYS,
    WARMUP_DAYS,
    WINDOW_START,
    DebtLedger,
    assemble_snapshot,
)
from engine.decide import decide
from engine.forecast import HORIZON_DAYS
from engine.models import ZERO, Action, ReasonCode, UserPolicy, money
from engine.outcome import Outcome, Realized, grade
from sim.household import History, HouseholdSpec, TxnKind, generate

# Refusals whose cost is a **deferral**, not a loss. The money moves later; it does not evaporate.
# Counting these in `false_refusal_cost` would price a correct refusal as a failure.
DEFERRING_REASONS = frozenset(
    {
        # The money moves next week. Same dollars, later.
        ReasonCode.CADENCE_HOLD,
        # Resolves the moment the household attests, or connects the card we can see a payment to.
        ReasonCode.CARD_COVERAGE_INCOMPLETE,
        # Resolves by itself, after three observed cycles.
        ReasonCode.CARD_BEHAVIOR_UNKNOWN,
    }
)


@dataclass(frozen=True)
class Graded:
    """One decision and its outcome, with the deferral question already answered."""

    day: date
    outcome: Outcome
    deferred: bool

    @property
    def real_cost(self) -> Decimal:
        """`false_refusal_cost`, with deferrals excluded.

        A deferral is not conservatism. It is a rule we chose, doing what we chose it for.
        """
        return ZERO if self.deferred else self.outcome.false_refusal_cost


@dataclass(frozen=True)
class Calibration:
    """What the engine's own errors actually look like. The asset that compounds.

    This is the number the spend-model change is gated on. Not an opinion about the forecast —
    a measurement of it.
    """

    graded_days: int
    # prd.md §5.2's guardrail, which outranks the primary KPI: did *we* overdraft anyone?
    sweep_caused_overdrafts: int
    # Days our projection was OPTIMISTIC — the realized low came in below what we projected.
    # This is the tail that decides the company, and it is the only number that licenses a
    # looser reserve.
    optimistic_days: int
    worst_projection_error: Decimal
    # Our cost of conservatism, deferrals excluded. See DEFERRING_REASONS.
    total_false_refusal_cost: Decimal
    # What we *deferred* rather than lost, kept separate so it can never be mistaken for the above.
    total_deferred: Decimal

    @property
    def breach_rate(self) -> float:
        """The fraction of graded days on which we were optimistic.

        **This is the dial.** The spend model may be loosened exactly as far as this number
        licenses, and not one basis point further.
        """
        return self.optimistic_days / self.graded_days if self.graded_days else 0.0

    @property
    def sweep_caused_overdraft_rate(self) -> float:
        return self.sweep_caused_overdrafts / self.graded_days if self.graded_days else 0.0


def realized_from(history: History, today: date) -> Realized:
    """The household's own cash movement over the horizon — **excluding anything we did**.

    `sim/` knows nothing about our sweeps, so its history is already in the canonical form
    `grade()` wants. In live mode this is where our settled sweeps would be added *back*.

    Card charges are excluded, because they are not checking outflows. The `CARD_PAYMENT` txn is
    where that money actually leaves, and it is in here.
    """
    net: dict[date, Decimal] = {}
    for i in range(1, HORIZON_DAYS + 1):
        day = today + timedelta(days=i)
        net[day] = sum(
            (t.amount for t in history.txns if t.day == day and t.kind is not TxnKind.CARD_CHARGE),
            ZERO,
        )
    return Realized(net_by_day=net)


def replay(
    spec: HouseholdSpec = DEMO_SPEC,
    start: date = WINDOW_START,
    days: int = SERVED_DAYS,
    seed: int = SEED,
    policy: UserPolicy = DEMO_POLICY,
    warmup: int = WARMUP_DAYS,
) -> Iterator[Graded]:
    """Walk a household day by day, decide, and grade — for every day we can honestly grade.

    The walk carries the same state `precompute.build()` does — the debt ledger, the rolling week
    of sweeps, the days since the last one — and assembles each `Snapshot` through
    `precompute.assemble_snapshot()` rather than rebuilding one. Two copies of that assembly would
    drift, and the calibration would start describing an engine that never shipped.

    **The engine's own sweeps are applied to the walk.** A replay that assumed we never acted would
    never trip `CADENCE_HOLD`, never observe a deferral, and would find surplus on days when — had
    we actually been running — we had already moved the money. It would be measuring a different
    engine, flatteringly.

    What our sweeps are **not** in is `Realized`. That is the household's own movement, and
    `grade()` applies the decision's sweep to it itself, so the caller cannot forget.

    **Not every day is gradeable, and that is not a gap to paper over.** A blocking refusal never
    ran the forecast, so it carries no `projected_low_balance` — and `grade()` refuses it rather
    than inventing a zero that would look like a perfect forecast and quietly pull the whole error
    distribution toward the origin.
    """
    # The same warm-up runway `precompute.build()` walks. The engine refuses below 60 days of
    # history (`MIN_HISTORY_DAYS`), and those refusals are *blocking* — they never ran a forecast,
    # so they are not gradeable. Starting cold would throw away most of the window and, worse,
    # would grade a household whose card behaviour we had not yet observed for three cycles.
    total = warmup + days
    history = generate(spec, start, total + HORIZON_DAYS + 1, seed)

    ledger = DebtLedger(principal=spec.card.balance, apr=spec.card.apr)
    card_payments = {t.day: -t.amount for t in history.txns if t.kind is TxnKind.CARD_PAYMENT}

    sweeps: dict[date, Decimal] = {}
    swept_cumulative = ZERO

    for offset in range(total):
        today = start + timedelta(days=offset)
        yesterday = today - timedelta(days=1)

        # Yesterday's sweep settles: off the card, and out of the checking balance that `sim/` —
        # which knows nothing of our sweeps — still shows in full.
        if settled := sweeps.get(yesterday, ZERO):
            ledger.pay(settled)
            swept_cumulative += settled

        if payment := card_payments.get(today, ZERO):
            ledger.pay(payment)

        ledger.accrue(today)

        last_sweep = max(sweeps, default=None)
        snapshot = assemble_snapshot(
            history=history,
            today=today,
            spec=spec,
            policy=policy,
            ledger_balance=ledger.outstanding,
            checking=history.balance_on(today) - swept_cumulative,
            swept_this_week=sum(
                (a for d, a in sweeps.items() if today - timedelta(days=6) <= d <= yesterday),
                ZERO,
            ),
            days_since_last_sweep=(today - last_sweep).days if last_sweep else None,
        )

        decision = decide(snapshot)

        if decision.action is Action.SWEEP:
            sweeps[today] = decision.amount

        # The warm-up is walked so the ledger and the cadence are real by the time we start
        # grading — but it is not itself graded. Those days are the engine refusing for want of
        # history, which is true and is not a forecast error.
        if offset < warmup:
            continue

        if decision.projected_low_balance is None:
            # A blocking refusal. It never forecast anything, so there is nothing to grade.
            continue

        outcome = grade(snapshot, decision, realized_from(history, today))
        deferred = decision.action is Action.REFUSE and any(
            r.code in DEFERRING_REASONS for r in decision.reasons
        )

        yield Graded(day=today, outcome=outcome, deferred=deferred)


def calibrate(graded: list[Graded]) -> Calibration:
    """Roll up a replay into the one number the spend model is gated on."""
    if not graded:
        return Calibration(
            graded_days=0,
            sweep_caused_overdrafts=0,
            optimistic_days=0,
            worst_projection_error=ZERO,
            total_false_refusal_cost=ZERO,
            total_deferred=ZERO,
        )

    errors = [g.outcome.projection_error for g in graded]

    return Calibration(
        graded_days=len(graded),
        sweep_caused_overdrafts=sum(1 for g in graded if g.outcome.sweep_caused_overdraft),
        # Negative error = we were OPTIMISTIC: the money was not there after all.
        optimistic_days=sum(1 for e in errors if e < ZERO),
        worst_projection_error=min(errors),
        total_false_refusal_cost=money(sum((g.real_cost for g in graded), ZERO)),
        total_deferred=money(
            sum((g.outcome.false_refusal_cost for g in graded if g.deferred), ZERO)
        ),
    )

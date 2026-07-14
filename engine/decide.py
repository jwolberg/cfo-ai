"""The sweep decision.

Deterministic. No LLM, no clock, no network, no randomness. Given the same Snapshot
this function returns the same Decision forever — which is what makes a sweep
explainable to a customer, auditable to a regulator, and replayable in a backtest
after Plaid has rewritten the underlying history beneath us.

The decision emits `Reason` codes with parameters, never sentences. Prose lives in
engine/explain.py, and the LLM narrates *from* the codes rather than passing strings
through. It is never in the decision path.

Read the refusal gates below as the product, not as validation. Almost every hard
thing that happens to a real household shows up here as a reason to do nothing.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from engine.forecast import HORIZON_DAYS, conservative_low_balance, funding_account
from engine.interest import claimable_interest_avoided
from engine.models import (
    ZERO,
    AccountKind,
    Action,
    Card,
    CardPortfolio,
    ConnectionState,
    CoverageState,
    Debt,
    Decision,
    PaymentBehavior,
    Reason,
    ReasonCode,
    Snapshot,
    money,
)

# Below this, the ACH risk and the cognitive noise of a notification cost more than
# the interest saved. Not every dollar is worth moving.
MIN_SWEEP = money("1.00")

# A balance older than this is a guess, and we do not move money against a guess.
MAX_BALANCE_AGE_DAYS = 2

# Recurring-event detection needs to see roughly two cycles of a monthly obligation
# before it can claim to know anything.
MIN_HISTORY_DAYS = 60

# Coefficient of variation of monthly income. Above this the household's own buffer is
# a rational hedge against volatility we cannot forecast — and sweeping it is worse
# advice than doing nothing. We decline to serve rather than serve badly.
MAX_INCOME_VARIATION = 0.25

# Idle cash outside the funding account worth telling the user about.
MIN_IDLE_TO_MENTION = money("1000.00")


def _refuse(*reasons: Reason, low: Decimal | None = None) -> Decision:
    return Decision(
        action=Action.REFUSE,
        amount=ZERO,
        target_debt_id=None,
        reasons=reasons,
        projected_low_balance=low,
    )


def _blocking_reasons(s: Snapshot) -> list[Reason]:
    """Conditions under which no amount of surplus justifies moving money."""
    reasons: list[Reason] = []

    account = funding_account(s)

    # Gates apply to the account the money actually leaves. A stale savings balance
    # cannot overdraw checking, so refusing on it would be superstition, not safety.
    if account is None:
        return [Reason(ReasonCode.FUNDING_ACCOUNT_MISSING, {"account_id": s.funding_account_id})]

    if account.kind is not AccountKind.CHECKING:
        return [Reason(ReasonCode.FUNDING_ACCOUNT_NOT_CHECKING, {"kind": account.kind})]

    if account.connection is not ConnectionState.HEALTHY:
        reasons.append(Reason(ReasonCode.CONNECTION_UNHEALTHY, {"state": account.connection}))

    if account.balance_age_days > MAX_BALANCE_AGE_DAYS:
        reasons.append(Reason(ReasonCode.BALANCE_STALE, {"age_days": account.balance_age_days}))

    if s.history_days < MIN_HISTORY_DAYS:
        reasons.append(
            Reason(
                ReasonCode.INSUFFICIENT_HISTORY,
                {"have_days": s.history_days, "need_days": MIN_HISTORY_DAYS},
            )
        )

    if s.income_variation > MAX_INCOME_VARIATION:
        reasons.append(
            Reason(
                ReasonCode.INCOME_TOO_VARIABLE,
                {"variation": s.income_variation, "limit": MAX_INCOME_VARIATION},
            )
        )

    if s.today in s.policy.blackout_dates:
        reasons.append(Reason(ReasonCode.BLACKOUT, {"date": s.today}))

    if s.sweeps_in_flight > ZERO:
        reasons.append(Reason(ReasonCode.SWEEP_IN_FLIGHT, {"amount": s.sweeps_in_flight}))

    return reasons


def _cadence_hold(s: Snapshot) -> Reason | None:
    """Is it simply too soon since the last sweep?

    This is *not* in `_blocking_reasons`, and the placement is the whole design.

    A blocking reason short-circuits `decide()` before the forecast runs, so the refusal
    carries no `projected_low_balance` — and `engine/outcome.py:grade()` refuses to grade a
    decision without one, on the grounds that inventing a projection would corrupt the
    calibration distribution with zeros that look like perfect forecasts. Gate the cadence up
    there and six days in seven become ungradeable: the engine would still *observe* daily and
    still be *wrong* daily, but it would no longer be *measured* daily, and strategy.md §3's
    "empirical distribution of our own errors" — the only asset that compounds — would lose
    most of its data to a rule that has nothing to do with forecasting.

    So the forecast runs every day regardless. The cadence limits what we *do*, never what we
    *know*. Daily data, weekly money.
    """
    limit = s.policy.min_days_between_sweeps

    if limit <= 0 or s.days_since_last_sweep is None:
        return None

    if s.days_since_last_sweep >= limit:
        return None

    return Reason(
        ReasonCode.CADENCE_HOLD,
        {"days_since": s.days_since_last_sweep, "min_days": limit},
    )


def _idle_elsewhere(s: Snapshot) -> list[Reason]:
    """Money sitting outside the funding account.

    We will not sweep it — it isn't where the debit lands, and moving it is a second
    ACH we haven't earned the right to make. But staying silent while someone holds
    thousands in a savings account earning nothing and pays 24% on a card is its own
    kind of failure. Name it; don't act on it.
    """
    idle = sum(
        (a.balance for a in s.accounts if a.account_id != s.funding_account_id),
        ZERO,
    )

    if idle < MIN_IDLE_TO_MENTION:
        return []

    return [Reason(ReasonCode.IDLE_CASH_ELSEWHERE, {"amount": idle})]


def _as_debt(card: Card) -> Debt:
    """A `Card` seen through the old `Debt` shape, for `engine/interest.py`.

    A deliberate, temporary adapter and **not** a second source of truth: the reserve reads
    `Card` and only `Card`. This exists because the interest model still amortizes a balance
    that only ever shrinks, and teaching it about new charges is its own unit (0014 / U5). When
    that lands, this function goes with it.

    `interest_bearing_balance`, not `total_owed`: a transactor's unbilled charges are covered
    by the grace period and accrue nothing.
    """
    return Debt(
        debt_id=card.card_id,
        balance=card.interest_bearing_balance,
        minimum_payment=card.minimum_payment,
        minimum_due_date=card.statement_due_date,
        apr=card.apr,
        observed_monthly_payment=card.observed_monthly_payment,
    )


def _select_target(portfolio: CardPortfolio) -> tuple[Card | None, Reason | None]:
    """Highest effective APR wins — among the cards it is honest to sweep to at all.

    A **transactor is never a target.** They pay no interest: the grace period already does
    exactly what our sweep claims to do. Moving their cash onto a card they were going to clear
    anyway is a *prepayment, not a saving*, and taking a share of it (prd.md §7.2, "profit only
    on progress") would be charging for nothing. They are still fully reserved against — see
    `untouchable()` — they are simply not somewhere we can honestly send money.

    Returns (target, refusal_reason).
    """
    open_cards = [c for c in portfolio.cards if c.total_owed > ZERO]

    if not open_cards:
        return None, Reason(ReasonCode.NO_DEBT)

    targetable = [c for c in open_cards if c.behavior is not PaymentBehavior.TRANSACTOR]

    if not targetable:
        # Every card they hold is cleared in full each month. There is no interest here to
        # avoid, and the honest thing is to say so rather than move money for the look of it.
        return None, Reason(ReasonCode.NO_INTEREST_TO_AVOID, {"card_count": len(open_cards)})

    if len(targetable) == 1:
        # Nothing to rank, so a missing APR costs us nothing here.
        return targetable[0], None

    ranked = [c for c in targetable if c.apr is not None]

    if not ranked:
        # Paying the wrong card looks exactly like working while quietly destroying
        # the entire point. We would rather say so.
        return None, Reason(ReasonCode.APR_UNKNOWN, {"card_count": len(targetable)})

    return max(ranked, key=lambda c: c.apr), None


def _coverage_reasons(snapshot: Snapshot) -> list[Reason]:
    """Refusals that come from not being able to see the whole liability.

    `decide()` ranks by APR and sweeps to the winner. If a card exists that we cannot see, we
    will have done the optimal thing with money that was already spoken for — paid the *right*
    card while starving one we never knew about. Optimality within an incomplete portfolio is
    not optimality.

    Both signals block, by decision: `UNMATCHED_PAYMENT` is hard evidence, and `UNATTESTED`
    means nobody has told us the list is complete. Attestation is therefore a real onboarding
    gate, not a checkbox — a household that has not attested gets refusals, and that is the
    intended, safe behaviour.
    """
    portfolio = snapshot.portfolio
    out: list[Reason] = []

    if portfolio.coverage is not CoverageState.COMPLETE:
        out.append(
            Reason(
                ReasonCode.CARD_COVERAGE_INCOMPLETE,
                {
                    "coverage": portfolio.coverage.value,
                    "unmatched": len(portfolio.unmatched_card_payments),
                },
            )
        )

    unknown = [c for c in portfolio.cards if c.behavior is PaymentBehavior.UNKNOWN]
    if unknown:
        # We do not know what this card will take out of checking. That figure sets the reserve
        # *and* the interest claim; a guess is load-bearing twice over.
        out.append(Reason(ReasonCode.CARD_BEHAVIOR_UNKNOWN, {"card_count": len(unknown)}))

    return out


def _behavior_amount(card: Card, statement: Decimal) -> Decimal:
    """What this household pays against a statement of `statement`.

    The minimum is what the **issuer** will accept. This is what the **household** pays, and
    for two of the three behaviours those are very different numbers. Reserving the first while
    the second leaves checking is the entire bug.
    """
    if card.behavior is PaymentBehavior.TRANSACTOR:
        # They clear it. Reserving their $40 minimum against a $2,000 statement under-reserves
        # by $1,960 and overdraws them on the 20th.
        return statement

    if card.behavior is PaymentBehavior.MINIMUM_ONLY:
        return min(max(card.minimum_payment, ZERO), statement)

    # REVOLVER. They pay a habitual amount above the minimum — the counterfactual the interest
    # claim is already measured against. Never less than the minimum; never more than is owed.
    habitual = card.observed_monthly_payment or ZERO
    return min(max(max(card.minimum_payment, ZERO), habitual), statement)


def obligation_in_horizon(card: Card, horizon_end: date) -> Decimal:
    """What this card will actually take out of checking inside the horizon.

    **Two statements, not one.** This is the correction that the first design of this function
    did not have, and getting it wrong does not merely under-reserve — it opens a hole where
    the old code had none.

    The reserve this replaced was a *rolling forecast*: `precompute.py` recomputed
    `minimum_due_date` fresh every day, so it always pointed at the next due date and reserved
    the minimum on essentially every day of the cycle. `Card.statement_due_date` is the
    opposite — a fact about a statement that has *already closed*. Key the reserve on that
    alone and the moment it is paid, the next statement has not closed yet, the card looks like
    it owes nothing, and the reserve falls to **zero for the last third of every cycle** while
    charges pile onto it.

    Worse, `forecast.py` skips the CARD_PAYMENT event *unconditionally*, on the event tag
    alone. In that window neither side would account for the obligation: the forecast skipped
    it and the reserve omitted it. A double-miss, in the one direction §3 forbids.

    So term 2 exists, and it is what makes the forecast-skip safe. With a 30-day horizon and a
    >=21-day grace, it activates once the next close is within `horizon - grace` days — which is
    precisely the window term 1 leaves empty. The two terms tile the cycle with no gap.
    """
    total = ZERO

    # 1. The statement that has already closed. A known fact, legally due.
    if card.statement_due_date <= horizon_end:
        total += _behavior_amount(card, card.statement_balance)

    # 2. The statement that has NOT closed yet, but will close *and* come due inside the
    #    horizon. The term the naive design forgot.
    next_due = card.cycle.due_for(card.next_close_date)
    if next_due <= horizon_end:
        # `unbilled_balance` is what has posted *so far*; more will post before the close. An
        # under-stated future statement is the unsafe direction, so this is a floor and the
        # caller is expected to have projected it forward. See derive_card().
        total += _behavior_amount(card, card.unbilled_balance)

    return total


def untouchable(snapshot: Snapshot) -> tuple[Decimal, Decimal]:
    """The cash that is not ours to move: (buffer_floor, reserved_obligations).

    Exported because `engine/outcome.py` has to answer "how much was *actually* safe, in
    hindsight?" and it must apply the identical definition of untouchable cash. Two copies of
    this arithmetic would drift, and the day they drifted the calibration numbers would quietly
    start measuring a different engine than the one that shipped.

    A card's obligation is not surplus. Sweeping it and then missing it would mean causing the
    exact late fee we exist to prevent — and for a transactor, the obligation is the whole
    statement, not the minimum the issuer would settle for.

    The portfolio is the single authoritative source: the forecast deliberately skips
    CARD_PAYMENT events so this reserve is not counted twice (see EventKind). Every figure that
    feeds it is validated non-negative at construction, because the one direction this must
    never fail is "bad upstream data shrinks the reserve and buys a bigger sweep."
    """
    horizon_end = snapshot.today + timedelta(days=HORIZON_DAYS)
    reserved = sum(
        (obligation_in_horizon(card, horizon_end) for card in snapshot.portfolio.cards),
        ZERO,
    )

    return max(snapshot.policy.buffer_floor, ZERO), reserved


def apply_caps(
    snapshot: Snapshot, target: Card, available: Decimal
) -> tuple[Decimal, list[Reason]]:
    """Cut `available` down to what the user's guardrails and the card actually permit.

    Exported for the same reason as `untouchable()`: `engine/outcome.py` has to answer "what
    would we have swept if our forecast had been perfect?", and the honest answer applies the
    *same* ceilings. Without this, `false_refusal_cost` would blame us for the user's own
    `max_sweep` — reporting a huge cost of conservatism on a day the engine was not being
    conservative at all, merely obedient. prd.md §5.3 says **our** cost of conservatism, and
    a cap the user chose is not ours.
    """
    amount = available
    reasons: list[Reason] = []

    weekly_headroom = snapshot.policy.max_weekly_sweep - snapshot.swept_this_week

    if snapshot.policy.max_sweep < amount:
        amount = snapshot.policy.max_sweep
        reasons.append(Reason(ReasonCode.PER_SWEEP_CAP, {"cap": snapshot.policy.max_sweep}))

    if weekly_headroom < amount:
        amount = weekly_headroom
        reasons.append(
            Reason(
                ReasonCode.WEEKLY_CAP,
                {"cap": snapshot.policy.max_weekly_sweep, "already": snapshot.swept_this_week},
            )
        )

    if target.total_owed < amount:
        amount = target.total_owed
        reasons.append(Reason(ReasonCode.CLEARS_THE_CARD, {"debt_id": target.card_id}))

    return amount, reasons


def would_sweep(snapshot: Snapshot, low: Decimal) -> Decimal:
    """What `decide()` would have moved if the projected low had been `low`.

    The grader's counterfactual. Feed it the *realized* low and it answers: knowing what we now
    know, and obeying every guardrail the user set, how much should we have moved? The gap
    between that and what we actually moved is our forecast error priced in dollars — and
    nothing else. Returns ZERO when it would have refused on the money.
    """
    if _coverage_reasons(snapshot):
        # We would not have swept at all: we cannot see the whole liability. A hindsight
        # counterfactual that ignores that would price a refusal we were right to make as a
        # cost — and the grader would learn to talk us out of it.
        return ZERO

    target, cannot_rank = _select_target(snapshot.portfolio)
    if target is None or cannot_rank:
        return ZERO

    buffer_floor, reserved = untouchable(snapshot)
    available = low - buffer_floor - reserved

    if available < MIN_SWEEP:
        return ZERO

    amount, _ = apply_caps(snapshot, target, available)

    return amount if amount >= MIN_SWEEP else ZERO


def decide(snapshot: Snapshot) -> Decision:
    if blocking := _blocking_reasons(snapshot):
        return _refuse(*blocking)

    # Before the forecast, and before any surplus is computed: if we cannot see the whole
    # portfolio, no amount of visible surplus justifies moving money into part of it.
    if coverage := _coverage_reasons(snapshot):
        return _refuse(*coverage)

    low, low_day = conservative_low_balance(snapshot)

    target, cannot_rank = _select_target(snapshot.portfolio)
    if cannot_rank:
        return _refuse(cannot_rank, low=low)

    buffer_floor, reserved = untouchable(snapshot)
    available = low - buffer_floor - reserved

    projection = Reason(
        ReasonCode.PROJECTION,
        {"low": low, "low_day": low_day, "buffer": buffer_floor, "reserved": reserved},
    )

    # After the forecast, deliberately — see _cadence_hold. The projection rides along on the
    # refusal so the day is still gradeable and the cost of the cadence stays visible.
    if hold := _cadence_hold(snapshot):
        return _refuse(hold, projection, *_idle_elsewhere(snapshot), low=low)

    if available < MIN_SWEEP:
        return _refuse(
            Reason(
                ReasonCode.NO_SURPLUS,
                {"low": low, "low_day": low_day, "buffer": buffer_floor, "reserved": reserved},
            ),
            *_idle_elsewhere(snapshot),
            low=low,
        )

    amount, cap_reasons = apply_caps(snapshot, target, available)
    reasons = [projection, *cap_reasons]

    if amount < MIN_SWEEP:
        return _refuse(Reason(ReasonCode.BELOW_MIN_SWEEP, {"minimum": MIN_SWEEP}), low=low)

    # What the sweep saves — the number the user is told and the number the company is
    # graded on (prd.md §5.1). None when the APR is unknown or the card does not amortize,
    # in which case no claim is emitted at all rather than a guessed one.
    if (saved := claimable_interest_avoided(_as_debt(target), amount, snapshot.today)) is not None:
        reasons.append(
            Reason(ReasonCode.INTEREST_AVOIDED, {"amount": saved, "debt_id": target.card_id})
        )

    # What is already on the card for next month's bill. Not why we acted — but the household
    # cannot see this number anywhere else, and it is the one that decides their next statement.
    if target.unbilled_balance > ZERO:
        reasons.append(
            Reason(
                ReasonCode.UNBILLED_ACCRUING,
                {
                    "amount": target.unbilled_balance,
                    "due": target.cycle.due_for(target.next_close_date),
                },
            )
        )

    reasons.extend(_idle_elsewhere(snapshot))

    return Decision(
        action=Action.SWEEP,
        amount=amount,
        target_debt_id=target.card_id,
        reasons=tuple(reasons),
        projected_low_balance=low,
    )

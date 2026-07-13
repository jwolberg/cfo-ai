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

from datetime import timedelta
from decimal import Decimal

from engine.forecast import HORIZON_DAYS, conservative_low_balance, funding_account
from engine.interest import claimable_interest_avoided
from engine.models import (
    ZERO,
    AccountKind,
    Action,
    ConnectionState,
    Debt,
    Decision,
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


def _select_target(debts: tuple[Debt, ...]) -> tuple[Debt | None, Reason | None]:
    """Highest effective APR wins. Returns (target, refusal_reason)."""
    open_debts = [d for d in debts if d.balance > ZERO]

    if not open_debts:
        return None, Reason(ReasonCode.NO_DEBT)

    if len(open_debts) == 1:
        # Nothing to rank, so a missing APR costs us nothing here.
        return open_debts[0], None

    ranked = [d for d in open_debts if d.apr is not None]

    if not ranked:
        # Paying the wrong card looks exactly like working while quietly destroying
        # the entire point. We would rather say so.
        return None, Reason(ReasonCode.APR_UNKNOWN, {"card_count": len(open_debts)})

    return max(ranked, key=lambda d: d.apr), None


def decide(snapshot: Snapshot) -> Decision:
    if blocking := _blocking_reasons(snapshot):
        return _refuse(*blocking)

    low, low_day = conservative_low_balance(snapshot)

    target, cannot_rank = _select_target(snapshot.debts)
    if cannot_rank:
        return _refuse(cannot_rank, low=low)

    # The minimum payment is not surplus. Sweeping it and then missing it would mean
    # causing the exact late fee we exist to prevent.
    #
    # Debt is the single authoritative source for minimums: the forecast deliberately
    # skips DEBT_MINIMUM events so this reserve is not counted twice (see EventKind).
    # Debt.__post_init__ rejects a negative minimum_payment; the max() is belt-and-
    # braces, because the one direction this must never fail is "bad upstream data
    # shrinks the reserve and buys a bigger sweep."
    horizon_end = snapshot.today + timedelta(days=HORIZON_DAYS)
    reserved = sum(
        (max(d.minimum_payment, ZERO) for d in snapshot.debts if d.minimum_due_date <= horizon_end),
        ZERO,
    )

    buffer_floor = max(snapshot.policy.buffer_floor, ZERO)
    available = low - buffer_floor - reserved

    projection = Reason(
        ReasonCode.PROJECTION,
        {"low": low, "low_day": low_day, "buffer": buffer_floor, "reserved": reserved},
    )

    if available < MIN_SWEEP:
        return _refuse(
            Reason(
                ReasonCode.NO_SURPLUS,
                {"low": low, "low_day": low_day, "buffer": buffer_floor, "reserved": reserved},
            ),
            *_idle_elsewhere(snapshot),
            low=low,
        )

    amount = available
    reasons = [projection]

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

    if target.balance < amount:
        amount = target.balance
        reasons.append(Reason(ReasonCode.CLEARS_THE_CARD, {"debt_id": target.debt_id}))

    if amount < MIN_SWEEP:
        return _refuse(Reason(ReasonCode.BELOW_MIN_SWEEP, {"minimum": MIN_SWEEP}), low=low)

    # What the sweep saves — the number the user is told and the number the company is
    # graded on (prd.md §5.1). None when the APR is unknown or the card does not amortize,
    # in which case no claim is emitted at all rather than a guessed one.
    if (saved := claimable_interest_avoided(target, amount, snapshot.today)) is not None:
        reasons.append(
            Reason(ReasonCode.INTEREST_AVOIDED, {"amount": saved, "debt_id": target.debt_id})
        )

    reasons.extend(_idle_elsewhere(snapshot))

    return Decision(
        action=Action.SWEEP,
        amount=amount,
        target_debt_id=target.debt_id,
        reasons=tuple(reasons),
        projected_low_balance=low,
    )

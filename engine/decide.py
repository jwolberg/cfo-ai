"""The sweep decision.

Deterministic. No LLM, no clock, no network, no randomness. Given the same Snapshot
this function returns the same Decision forever — which is what makes a sweep
explainable to a customer, auditable to a regulator, and replayable in a backtest
after Plaid has rewritten the underlying history beneath us.

The LLM narrates `Decision.reasons`. It never produces them.

Read the refusal gates below as the product, not as validation. Almost every hard
thing that happens to a real household shows up here as a reason to do nothing.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from engine.forecast import HORIZON_DAYS, conservative_low_balance, funding_account
from engine.models import (
    ZERO,
    AccountKind,
    Action,
    ConnectionState,
    Debt,
    Decision,
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


def _refuse(*reasons: str, low: Decimal | None = None) -> Decision:
    return Decision(
        action=Action.REFUSE,
        amount=ZERO,
        target_debt_id=None,
        reasons=tuple(reasons),
        projected_low_balance=low,
    )


def _blocking_reasons(s: Snapshot) -> list[str]:
    """Conditions under which no amount of surplus justifies moving money."""
    reasons: list[str] = []

    account = funding_account(s)

    # Gates apply to the account the money actually leaves. A stale savings balance
    # cannot overdraw checking, so refusing on it would be superstition, not safety.
    if account is None:
        return ["We can't see the account the payment would come from."]

    if account.kind is not AccountKind.CHECKING:
        return ["Payments have to come from a checking account."]

    if account.connection is not ConnectionState.HEALTHY:
        reasons.append("Your checking account needs reconnecting — we can't see your real balance.")

    if account.balance_age_days > MAX_BALANCE_AGE_DAYS:
        reasons.append("Your balance is stale, so we're not acting on it today.")

    if s.history_days < MIN_HISTORY_DAYS:
        reasons.append(
            f"We only have {s.history_days} days of history. We're still learning your "
            "pattern and won't move money until we know it."
        )

    if s.income_variation > MAX_INCOME_VARIATION:
        reasons.append(
            "Your income is too variable for us to promise a payment is safe. "
            "Holding your cash is the right call — that buffer is doing real work."
        )

    if s.today in s.policy.blackout_dates:
        reasons.append("You've paused sweeps for today.")

    if s.sweeps_in_flight > ZERO:
        reasons.append(
            f"A ${s.sweeps_in_flight} payment hasn't settled yet. We won't stack another "
            "on top of money the bank may not have taken out yet."
        )

    return reasons


def _idle_elsewhere(s: Snapshot) -> list[str]:
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

    return [
        f"Separately: you're holding ${idle} in savings. It isn't cash we can move from "
        "here, and some of it should stay as your buffer — but a buffer that size belongs "
        "somewhere it earns interest, not in an account paying nothing while your card "
        "charges you."
    ]


def _select_target(debts: tuple[Debt, ...]) -> tuple[Debt | None, str | None]:
    """Highest effective APR wins. Returns (target, refusal_reason)."""
    open_debts = [d for d in debts if d.balance > ZERO]

    if not open_debts:
        return None, "You have no debt left to pay. Nothing to do — congratulations."

    if len(open_debts) == 1:
        # Nothing to rank, so a missing APR costs us nothing here.
        return open_debts[0], None

    ranked = [d for d in open_debts if d.apr is not None]

    if not ranked:
        # Paying the wrong card looks exactly like working while quietly destroying
        # the entire point. We would rather say so.
        return None, (
            "We don't have the APR for your cards, so we can't tell which one is "
            "costing you the most. Add them and we'll start."
        )

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
    # Debt.__post_init__ already rejects a negative minimum_payment — the max() is
    # belt-and-braces, because the one direction this must never fail is "bad upstream
    # data shrinks the reserve and buys a bigger sweep."
    horizon_end = snapshot.today + timedelta(days=HORIZON_DAYS)
    reserved = sum(
        (max(d.minimum_payment, ZERO) for d in snapshot.debts if d.minimum_due_date <= horizon_end),
        ZERO,
    )

    available = low - max(snapshot.policy.buffer_floor, ZERO) - reserved

    if available < MIN_SWEEP:
        idle = _idle_elsewhere(snapshot)
        return _refuse(
            f"Your balance is projected to dip to ${low} on {low_day}. After your "
            f"${snapshot.policy.buffer_floor} buffer and ${reserved} of minimum payments, "
            "there's nothing spare in checking. We're leaving your cash alone.",
            *idle,
            low=low,
        )

    amount = available
    reasons = [
        f"Your balance is projected to bottom out at ${low} on {low_day}, "
        f"after your ${snapshot.policy.buffer_floor} buffer and ${reserved} of minimums.",
    ]

    weekly_headroom = snapshot.policy.max_weekly_sweep - snapshot.swept_this_week

    if snapshot.policy.max_sweep < amount:
        amount = snapshot.policy.max_sweep
        reasons.append(f"Held to your ${snapshot.policy.max_sweep} per-payment cap.")

    if weekly_headroom < amount:
        amount = weekly_headroom
        reasons.append(f"Held to your ${snapshot.policy.max_weekly_sweep} weekly cap.")

    if target.balance < amount:
        amount = target.balance
        reasons.append("That clears the card.")

    if amount < MIN_SWEEP:
        return _refuse("Nothing left to move under your caps this week.", low=low)

    return Decision(
        action=Action.SWEEP,
        amount=amount,
        target_debt_id=target.debt_id,
        reasons=tuple(reasons),
        projected_low_balance=low,
    )

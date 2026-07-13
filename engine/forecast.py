"""Conservative cash-flow projection.

One rule governs everything here:

    Money arrives late and small. Money leaves early and large.

Every uncertain quantity is resolved toward the end of its range that hurts the user.
This is not pessimism for its own sake — it is the only asymmetry that makes a
probabilistic forecast safe to bet someone's rent on. A forecast that is right on
average will overdraft half its users half the time.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from engine.models import ZERO, Account, EventKind, Snapshot

HORIZON_DAYS = 30

# An inflow we are less sure of than this is not counted as money at all.
INCOME_CONFIDENCE_FLOOR = 0.80


def funding_account(snapshot: Snapshot) -> Account | None:
    for a in snapshot.accounts:
        if a.account_id == snapshot.funding_account_id:
            return a
    return None


def conservative_low_balance(snapshot: Snapshot) -> tuple[Decimal, date]:
    """Return (low_balance, day_it_occurs) for the *funding account* over the horizon.

    Only the funding account is projected, and this is the whole point. An ACH debit
    leaves one specific account. A user with $100 in checking and $5,000 in savings has
    $5,100 of money and $100 of *protection* — summing them and testing the total
    against the buffer would authorize a sweep that overdraws checking while the savings
    sits there untouched. Savings is real money, but it is not *there*, and moving it is
    a second ACH with its own delay and its own failure modes.

    The low balance — not the ending balance — is what constrains a sweep. The user only
    has to be broke once.
    """
    account = funding_account(snapshot)
    if account is None:
        raise ValueError(f"funding account {snapshot.funding_account_id!r} not in snapshot")

    balance = account.balance

    # Pending debits against this account have not posted, but the money is already
    # spoken for. Pending *credits* are ignored: we do not spend money the bank has not
    # handed over.
    for p in snapshot.pending:
        if p.account_id == account.account_id and p.amount < ZERO:
            balance += p.amount

    # A sweep we initiated but that has not settled may not be reflected in the
    # balance yet. Subtract it, or we will spend the same dollar twice.
    balance -= snapshot.sweeps_in_flight

    daily: dict[date, Decimal] = {}

    for event in snapshot.events:
        if event.account_id != account.account_id:
            # Income paid into savings does not protect a checking-account debit.
            continue

        if event.kind is EventKind.DEBT_MINIMUM:
            # Already reserved from available cash by decide(), out of Debt — the
            # authoritative source. Subtracting it here too would double-count it.
            continue

        if event.is_inflow:
            if event.confidence < INCOME_CONFIDENCE_FLOOR:
                continue  # not money — a hope
            # Late and small.
            when = event.expected_date + timedelta(days=event.date_jitter_days)
            amount = min(event.amount, event.amount_low)
        else:
            # Early and large. Outflows are counted regardless of confidence: an
            # obligation we are unsure of is still an obligation we might owe.
            when = event.expected_date - timedelta(days=event.date_jitter_days)
            if when < snapshot.today:
                when = snapshot.today
            amount = min(event.amount, event.amount_high)  # more negative = larger

        daily[when] = daily.get(when, ZERO) + amount

    low = balance
    low_day = snapshot.today

    for offset in range(HORIZON_DAYS + 1):
        day = snapshot.today + timedelta(days=offset)
        balance += daily.get(day, ZERO)
        if offset > 0:
            # Assume the user spends at the high end of their discretionary range,
            # every remaining day of the horizon.
            balance -= snapshot.daily_discretionary_high

        if balance < low:
            low = balance
            low_day = day

    return low, low_day

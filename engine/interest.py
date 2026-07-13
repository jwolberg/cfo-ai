"""What the debt costs, and what a sweep saves.

This module computes the number in `prd.md` §1 — *"that's $31 of interest you won't pay"* —
and the number in §5.1 that the company is graded on. They are the same number, and they
must never disagree, so there is exactly one place that computes it.

Deterministic, like the rest of the engine: no clock, no network, no randomness. `start` is
an input.

## The model

Interest accrues **daily** on the outstanding principal at `apr / 365`, with no intra-cycle
compounding — the average-daily-balance method that revolving cards actually use. Accrued
interest is **posted** to the balance at each statement close, and that is the one and only
place a figure is rounded (via `money()`, `ROUND_HALF_EVEN`, matching the repo). Payments
land at the start of their day, before that day's interest accrues.

Daily accrual is not incidental. This product sweeps **every day**, so *when in the cycle* a
dollar lands is exactly the thing we are being paid to get right. A monthly-periodic-rate
amortization — the textbook one — would value a sweep on day 2 and a sweep on day 29
identically, and it would be wrong in the direction of over-claiming, on a figure we put in
front of the customer.

## The counterfactual is the user's own trajectory, and it is an argument

`prd.md` §5.1: interest avoided is *"measured against a stated counterfactual (the user's own
pre-signup payment trajectory)."* Not the card minimum. Users of this product already pay
more than the minimum — that is *why* they have idle cash — and crediting our sweep with
interest they were never going to pay anyway inflates the one number the company is graded
on. §5.1 exists to ban exactly that.

So the counterfactual payment is passed in explicitly. `Debt.minimum_payment` is **not an
input to this module at all**, and a test asserts that changing it does not move the claim by
a cent.

## Three ways we decline to answer

- **No APR** → `None`. Plaid does not report it for many issuers (§6.3). A figure that looks
  computed but was invented is worse than no figure.
- **No observed payment history** → `None`, from `claimable_interest_avoided`. We do *not*
  fall back to the minimum; that is the flattering assumption above. In practice this costs
  nothing: `INSUFFICIENT_HISTORY` already refuses to sweep below 60 days, so by the time we
  may move money we have seen two payment cycles.
- **The payments do not cover the interest** → raises. There is no payoff, so there is no
  total interest, and no honest answer exists. That household is drowning; the model says so
  rather than returning a plausible number.

The with-sweep path applies **this one sweep** and then assumes the household's ordinary
payments continue. It does not assume we go on sweeping daily — we do not book credit for
actions we have not taken.
"""

from __future__ import annotations

import calendar
from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal

from engine.models import ZERO, Debt, money

DAYS_PER_YEAR = Decimal("365")

# A backstop, not the primary guard — `_check_amortizing` catches a non-amortizing debt at
# the first statement. This only fires on a debt that shrinks by fractions of a cent a cycle.
MAX_CYCLES = 600  # 50 years


def _statement_day(year: int, month: int, day_of_month: int) -> date:
    """The statement close in a given month, clamped to the month's length.

    A card that closes on the 31st closes on the 28th in February. Left unclamped this
    raises, and a February crash in the interest model is not a failure mode worth having.
    """
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day_of_month, last))


def _next_close(after: date, day_of_month: int) -> date:
    """The first statement close on or after `after`."""
    close = _statement_day(after.year, after.month, day_of_month)
    if close >= after:
        return close

    year, month = (after.year + 1, 1) if after.month == 12 else (after.year, after.month + 1)
    return _statement_day(year, month, day_of_month)


def total_interest(
    debt: Debt,
    start: date,
    monthly_payment: Decimal,
    extra: Mapping[date, Decimal] | None = None,
) -> Decimal | None:
    """Total interest paid from `start` until the debt clears, paying `monthly_payment`.

    `monthly_payment` is the counterfactual — what the household actually pays each cycle.
    It is deliberately *not* read from `Debt.minimum_payment`; see the module docstring.

    `extra` is money paid beyond that, keyed by the day it lands. A sweep is one entry in it.

    Returns `None` when the APR is unknown. Raises `ValueError` when the payments never
    cover the accruing interest — the balance grows without bound, there is no payoff, and
    therefore no total to return.
    """
    if debt.apr is None:
        return None

    payments = dict(extra or {})
    daily_rate = debt.apr / DAYS_PER_YEAR

    balance = debt.balance
    accrued = ZERO  # unrounded until it posts
    paid = ZERO

    day = start
    close = _next_close(start, debt.minimum_due_date.day)
    balance_at_last_close = balance
    cycles = 0

    while True:
        # Payments land before the day's interest accrues — a dollar paid today does not
        # accrue today. This is what makes a sweep on day 2 worth more than one on day 29.
        if payment := payments.get(day):
            balance -= payment

        if day == close:
            posted = money(accrued)
            balance += posted
            paid += posted
            accrued = ZERO

            balance -= min(monthly_payment, balance)

            if balance > ZERO:
                cycles += 1
                _check_amortizing(debt, monthly_payment, balance, balance_at_last_close, cycles)
                balance_at_last_close = balance

            close = _next_close(close + timedelta(days=1), debt.minimum_due_date.day)

        if balance <= ZERO:
            return paid

        accrued += balance * daily_rate
        day += timedelta(days=1)


def _check_amortizing(
    debt: Debt,
    monthly_payment: Decimal,
    balance: Decimal,
    previous: Decimal,
    cycles: int,
) -> None:
    """Refuse to loop forever on a debt that cannot be paid off.

    A household whose payments do not cover their interest has no payoff date. That is worth
    detecting loudly — it is the single most important fact about their finances — not
    smoothing into a plausible-looking number.
    """
    if balance >= previous:
        raise ValueError(
            f"debt {debt.debt_id!r} does not amortize: a payment of {monthly_payment} does "
            f"not cover the interest accruing on a balance of {balance} at {debt.apr}"
        )

    if cycles >= MAX_CYCLES:
        raise ValueError(
            f"debt {debt.debt_id!r} does not amortize within {MAX_CYCLES} statement cycles"
        )


def interest_avoided(
    debt: Debt,
    sweep: Decimal,
    on: date,
    monthly_payment: Decimal,
) -> Decimal | None:
    """What paying `sweep` on day `on` saves, against the household's ordinary payments alone.

    Returns `None` when the APR is unknown. A sweep larger than the balance saves exactly
    what clearing the balance saves and no more — overpaying a card does not buy extra
    interest savings and must not claim to.
    """
    if sweep < ZERO:
        raise ValueError(f"sweep={sweep} cannot be negative")

    counterfactual = total_interest(debt, start=on, monthly_payment=monthly_payment)
    if counterfactual is None:
        return None

    with_sweep = total_interest(debt, start=on, monthly_payment=monthly_payment, extra={on: sweep})
    assert with_sweep is not None  # same debt, same APR

    return money(counterfactual - with_sweep)


def claimable_interest_avoided(debt: Debt, sweep: Decimal, on: date) -> Decimal | None:
    """`interest_avoided`, for the decision path — returns `None` instead of raising.

    Two layers, deliberately, because they answer different questions:

    - `interest_avoided` is the **model**. Asked for the interest on a debt that has no
      payoff, it raises, because there is no honest answer and a caller that wanted one has
      a bug.
    - This is the **product**. `decide()` must be total: a card with bad data, or a household
      underwater on their own payments, is not a reason to crash their daily decision. The
      sweep is still correct and still safe — we simply cannot say what it saved.

    `None` here means exactly one thing: *no claim can honestly be made* — because the APR is
    unknown, because we have not observed what they were paying, or because their payments do
    not amortize the debt. The caller emits no `INTEREST_AVOIDED` reason, and so there is no
    code path anywhere that can render an invented number.
    """
    if debt.observed_monthly_payment is None:
        return None

    try:
        return interest_avoided(debt, sweep, on, debt.observed_monthly_payment)
    except ValueError:
        return None

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

from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal

from engine.models import ZERO, AprSource, Card, PaymentBehavior, money, statement_day

DAYS_PER_YEAR = Decimal("365")

# A backstop, not the primary guard — `_check_amortizing` catches a non-amortizing debt at
# the first statement. This only fires on a debt that shrinks by fractions of a cent a cycle.
MAX_CYCLES = 600  # 50 years


# Moved to engine/models.py, where StatementCycle also needs it. Kept as an alias rather than
# a second copy: two implementations of a February clamp would drift, and the bug would show
# up in exactly one of them.
_statement_day = statement_day


def _next_close(after: date, day_of_month: int) -> date:
    """The first statement close on or after `after`."""
    close = _statement_day(after.year, after.month, day_of_month)
    if close >= after:
        return close

    year, month = (after.year + 1, 1) if after.month == 12 else (after.year, after.month + 1)
    return _statement_day(year, month, day_of_month)


def total_interest(
    card: Card,
    start: date,
    monthly_payment: Decimal,
    extra: Mapping[date, Decimal] | None = None,
) -> Decimal | None:
    """Total interest paid from `start` until the card clears, paying `monthly_payment`.

    `monthly_payment` is the counterfactual — what the household actually pays each cycle.
    It is deliberately *not* read from `card.minimum_payment`; see the module docstring.

    `extra` is money paid beyond that, keyed by the day it lands. A sweep is one entry in it.

    ## The card gets charged, and until now this model denied it

    Every cycle, `card.observed_monthly_charges` lands back on the balance. Without that, this
    function projected a balance that could only ever *shrink* — so a household charging
    $1,500/month to the card we are sweeping against got a payoff date that never arrives and
    an interest-avoided figure overstated by construction. That is the number prd.md §5.1 says
    the company is graded on.

    Charges are posted **at the close, before the payment**, not daily. That understates the
    days they spend accruing, so it understates the interest, so it understates what we claim
    to have saved. Wrong in the safe direction, deliberately: the alternative is a model that
    flatters us on the one figure we are paid on.

    Returns `None` when the APR is unknown **or merely estimated**, and **`ZERO` for a
    TRANSACTOR** — they hold the grace period, so they pay no interest at all and there is
    nothing for a sweep to save. Raises `ValueError` when the payments never cover the interest
    *and* the new charges: the balance grows without bound, there is no payoff, and no honest
    total exists.
    """
    if card.apr is None:
        return None

    if card.apr_source is AprSource.ESTIMATED:
        # We defaulted this rate because the issuer would not report it (`AprSource`, ticket
        # 0028). The engine is allowed to **rank** on that guess — a wrong target optimizes worse
        # and overdraws nobody — but it may not **price** it.
        #
        # This function computes the number `prd.md` §5.1 grades the company on and the sentence
        # §1 shows the user: "that's $31 of interest you won't pay." Both would be arithmetic on
        # a rate we invented. §5.1 already banned the *projected* KPI for flattering us; billing
        # against a guessed APR is the same disease with a different symptom.
        #
        # So the sweep still happens and the feed simply says nothing about what it saved. That
        # is the honest output, and it is what archetype D exists to show.
        return None

    if card.behavior is PaymentBehavior.TRANSACTOR:
        # The grace period already does exactly what our sweep claims to do. They pay nothing,
        # so there is nothing to avoid — and a "saving" we report here would be a prepayment we
        # then charged them a share of (prd.md §7.2).
        return ZERO

    payments = dict(extra or {})
    daily_rate = card.apr / DAYS_PER_YEAR
    charges = card.observed_monthly_charges or ZERO

    balance = card.interest_bearing_balance
    accrued = ZERO  # unrounded until it posts
    paid = ZERO

    day = start
    close = _next_close(start, card.statement_due_date.day)
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

            # What they put on the card this cycle. A REVOLVER has no grace period, so these
            # begin accruing immediately — which is exactly why a card can outrun its payments.
            balance += charges

            balance -= min(monthly_payment, balance)

            if balance > ZERO:
                cycles += 1
                _check_amortizing(card, monthly_payment, balance, balance_at_last_close, cycles)
                balance_at_last_close = balance

            close = _next_close(close + timedelta(days=1), card.statement_due_date.day)

        if balance <= ZERO:
            return paid

        accrued += balance * daily_rate
        day += timedelta(days=1)


def _check_amortizing(
    card: Card,
    monthly_payment: Decimal,
    balance: Decimal,
    previous: Decimal,
    cycles: int,
) -> None:
    """Refuse to loop forever on a card that cannot be paid off.

    A household whose payments do not cover their interest **and their new charges** has no
    payoff date. That is worth detecting loudly — it is the single most important fact about
    their finances — not smoothing into a plausible-looking number.

    This has always been here. What is new is that it can finally *fire* for the right reason:
    until charges entered the model, a balance could only shrink, so the one household this
    check exists to protect was invisible to it. For them the sweep is not the answer, and the
    honest output is no figure at all plus a sentence saying so.
    """
    if balance >= previous:
        raise ValueError(
            f"card {card.card_id!r} does not amortize: a payment of {monthly_payment} does not "
            f"cover the interest on {balance} at {card.apr} plus "
            f"{card.observed_monthly_charges or ZERO} of new charges each cycle"
        )

    if cycles >= MAX_CYCLES:
        raise ValueError(
            f"card {card.card_id!r} does not amortize within {MAX_CYCLES} statement cycles"
        )


def interest_avoided(
    card: Card,
    sweep: Decimal,
    on: date,
    monthly_payment: Decimal,
) -> Decimal | None:
    """What paying `sweep` on day `on` saves, against the household's ordinary payments alone.

    Returns `None` when the APR is unknown, and **exactly `$0.00` for a TRANSACTOR**: their
    grace period already does what the sweep claims to do, so a sweep is a *prepayment*, not a
    saving. Zero is not a rounding artefact here — it is the answer.

    A sweep larger than the balance saves exactly what clearing the balance saves and no more —
    overpaying a card does not buy extra interest savings and must not claim to.
    """
    if sweep < ZERO:
        raise ValueError(f"sweep={sweep} cannot be negative")

    counterfactual = total_interest(card, start=on, monthly_payment=monthly_payment)
    if counterfactual is None:
        return None

    with_sweep = total_interest(card, start=on, monthly_payment=monthly_payment, extra={on: sweep})
    assert with_sweep is not None  # same card, same APR

    return money(counterfactual - with_sweep)


def claimable_interest_avoided(card: Card, sweep: Decimal, on: date) -> Decimal | None:
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
    if card.observed_monthly_payment is None:
        return None

    try:
        return interest_avoided(card, sweep, on, card.observed_monthly_payment)
    except ValueError:
        # Their charges outrun their payments. The card grows, there is no payoff, and no
        # honest figure exists — so we make no claim, and `decide()` emits no reason. For this
        # household the sweep is not the answer, and the dashboard should say so (U7).
        return None

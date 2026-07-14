"""Adversarial tests for the sweep decision engine.

These are the spec. Each test is a way the real world breaks the happy path, and the
correct behaviour in almost every one of them is *refuse to move money*.

The engine's job is not to find the largest safe payment. It is to be right about the
word "safe" — and the cheapest way to be right is to decline.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from engine.decide import MIN_SWEEP, decide, obligation_in_horizon
from engine.forecast import HORIZON_DAYS
from engine.models import (
    MIN_GRACE_DAYS,
    ZERO,
    Account,
    AccountKind,
    Action,
    Card,
    CardPortfolio,
    CashEvent,
    ConnectionState,
    CoverageState,
    Debt,
    EventKind,
    PaymentBehavior,
    PendingTransaction,
    ReasonCode,
    Snapshot,
    StatementCycle,
    UnmatchedPayment,
    UserPolicy,
    money,
)

TODAY = date(2026, 7, 12)


def account(
    balance: str,
    *,
    connection=ConnectionState.HEALTHY,
    age=0,
    account_id="chk",
    kind=AccountKind.CHECKING,
) -> Account:
    return Account(
        account_id=account_id,
        balance=money(balance),
        connection=connection,
        balance_age_days=age,
        kind=kind,
    )


def savings(balance: str, *, account_id="sav", **kw) -> Account:
    return account(balance, account_id=account_id, kind=AccountKind.SAVINGS, **kw)


def paycheck(
    day_offset: int, amount: str = "3000.00", *, confidence=0.95, jitter=1, account_id="chk"
) -> CashEvent:
    return CashEvent(
        label="Payroll",
        account_id=account_id,
        expected_date=TODAY + timedelta(days=day_offset),
        amount=money(amount),
        amount_low=money(amount),
        amount_high=money(amount),
        date_jitter_days=jitter,
        confidence=confidence,
    )


def bill(
    day_offset: int,
    amount: str,
    label: str = "Rent",
    *,
    high: str | None = None,
    account_id="chk",
) -> CashEvent:
    amt = money("-" + amount.lstrip("-"))
    return CashEvent(
        label=label,
        account_id=account_id,
        expected_date=TODAY + timedelta(days=day_offset),
        amount=amt,
        amount_low=amt,
        amount_high=money("-" + (high or amount).lstrip("-")),
        date_jitter_days=1,
        confidence=0.95,
    )


def card(
    apr: str | None = "0.2399",
    balance: str = "9000.00",
    debt_id: str = "visa",
    observed: str | None = "400.00",
    behavior: PaymentBehavior = PaymentBehavior.REVOLVER,
    unbilled: str = "0.00",
) -> Card:
    """A revolver carrying a balance, paying well above the minimum.

    The default deliberately has **no unbilled charges** and a statement due inside the
    horizon, so the reserve is the familiar single term and the existing tests keep testing
    what they were written to test. The two-statement behaviour has its own tests below.
    """
    return Card(
        card_id=debt_id,
        # Deliberately Decimal(), not money(): an APR is a rate, not a dollar amount, and
        # money() quantizes to cents — money("0.2399") is 0.24, a different card. Harmless
        # while APR is only ranked, wrong the moment it is multiplied by a balance.
        apr=Decimal(apr) if apr is not None else None,
        cycle=StatementCycle(close_day_of_month=20, grace_days=MIN_GRACE_DAYS),
        statement_balance=money(balance),
        statement_due_date=TODAY + timedelta(days=20),
        minimum_payment=money("180.00"),
        unbilled_balance=money(unbilled),
        # Far enough out that its due date (close + 21) sits beyond the 30-day horizon, so
        # term 2 of the reserve is dormant unless a test asks for it.
        next_close_date=TODAY + timedelta(days=25),
        behavior=behavior,
        # What this household was already paying, well above the $180 minimum — which is
        # exactly why they have idle cash to sweep. This is the counterfactual the interest
        # claim is measured against (prd.md §5.1).
        observed_monthly_payment=money(observed) if observed is not None else None,
    )


def wallet(*cards: Card, coverage: CoverageState = CoverageState.COMPLETE) -> CardPortfolio:
    """A portfolio we can fully see. Coverage gates have their own tests."""
    return CardPortfolio(cards=cards, coverage=coverage)


def policy(**kw) -> UserPolicy:
    base = dict(
        buffer_floor=money("750.00"),
        max_sweep=money("300.00"),
        max_weekly_sweep=money("600.00"),
    )
    base.update(kw)
    return UserPolicy(**base)


def snapshot(**kw) -> Snapshot:
    """A healthy, forecastable user with real surplus. Tests mutate one thing at a time."""
    base = dict(
        today=TODAY,
        accounts=(account("4000.00"),),
        funding_account_id="chk",
        events=(paycheck(3), bill(6, "1800.00")),
        pending=(),
        portfolio=wallet(card()),
        policy=policy(),
        daily_discretionary_high=money("40.00"),
        income_variation=0.05,
        history_days=180,
        sweeps_in_flight=money("0.00"),
        swept_this_week=money("0.00"),
    )
    base.update(kw)
    return Snapshot(**base)


# --- the happy path exists, and it is deliberately modest ------------------------


def test_sweeps_surplus_to_the_card_when_everything_is_healthy():
    d = decide(snapshot())

    assert d.action is Action.SWEEP
    assert d.target_debt_id == "visa"
    assert d.amount > MIN_SWEEP
    assert d.projected_low_balance is not None


def test_sweep_never_exceeds_the_user_cap():
    # Enormous balance, no obligations: surplus is huge, the cap still binds.
    d = decide(snapshot(accounts=(account("50000.00"),), events=()))

    assert d.action is Action.SWEEP
    assert d.amount == money("300.00")
    assert d.has(ReasonCode.PER_SWEEP_CAP)


def test_weekly_cap_binds_across_sweeps():
    d = decide(
        snapshot(accounts=(account("50000.00"),), events=(), swept_this_week=money("450.00"))
    )

    assert d.action is Action.SWEEP
    assert d.amount == money("150.00")  # 600 weekly - 450 already swept


def test_amount_is_never_more_than_the_debt_balance():
    d = decide(
        snapshot(
            accounts=(account("50000.00"),),
            events=(),
            portfolio=wallet(card(balance="42.00")),
        )
    )

    assert d.amount == money("42.00")


# --- the forecast is asymmetric on purpose ---------------------------------------


def test_income_is_assumed_late_and_obligations_early():
    """Money arrives late and small; it leaves early and large. Never the reverse.

    Rent nominally clears the day *after* payday. Under jitter the safe assumption is
    that rent lands early and the paycheck lands late — so for a stretch of days the
    user is carrying the rent without the income. The projected low must reflect that
    inversion, not the nominal ordering.
    """
    s = snapshot(
        accounts=(account("2500.00"),),
        events=(paycheck(5, "3000.00", jitter=2), bill(6, "2000.00")),
        daily_discretionary_high=money("0.00"),
    )

    d = decide(s)

    # 2500 in hand, rent (2000) may clear on day 5, paycheck may not arrive until day 7.
    assert d.projected_low_balance == money("500.00")
    # Below the 750 buffer, so nothing moves.
    assert d.action is Action.REFUSE


def test_uncertain_income_is_not_counted_at_all():
    """A paycheck we are only 50% sure of is not money. It is a hope."""
    certain = decide(snapshot(accounts=(account("2000.00"),), events=(paycheck(2, "5000.00"),)))
    uncertain = decide(
        snapshot(
            accounts=(account("2000.00"),),
            events=(paycheck(2, "5000.00", confidence=0.5),),
        )
    )

    assert certain.action is Action.SWEEP
    assert uncertain.action is Action.REFUSE


def test_a_bill_that_might_be_larger_is_assumed_larger():
    d = decide(
        snapshot(
            accounts=(account("3000.00"),),
            events=(bill(4, "1000.00", "Utilities", high="2400.00"),),
            daily_discretionary_high=money("0.00"),
        )
    )

    # 3000 - 2400 (the high end, not the expected 1000) = 600, under the 750 buffer.
    assert d.projected_low_balance == money("600.00")
    assert d.action is Action.REFUSE


def test_pending_debits_are_treated_as_already_gone():
    d = decide(
        snapshot(
            accounts=(account("1200.00"),),
            events=(),
            pending=(
                PendingTransaction(label="Car repair", account_id="chk", amount=money("-400.00")),
            ),
            daily_discretionary_high=money("0.00"),
        )
    )

    # 1200 - 400 pending = 800; buffer is 750, leaving 50 — but minimums must be
    # protected too, so there is nothing to sweep.
    assert d.action is Action.REFUSE
    assert d.has(ReasonCode.NO_SURPLUS)


def test_discretionary_spending_is_charged_every_day_of_the_horizon():
    without = decide(
        snapshot(accounts=(account("5000.00"),), events=(), daily_discretionary_high=money("0.00"))
    )
    with_spend = decide(
        snapshot(
            accounts=(account("5000.00"),), events=(), daily_discretionary_high=money("100.00")
        )
    )

    assert without.projected_low_balance == money("5000.00")
    # 30 days x $100 of assumed spend.
    assert with_spend.projected_low_balance == money("2000.00")


# --- the sweep leaves ONE account, so only that account's balance protects it -------


def test_savings_is_not_spendable_cash():
    """The bug this suite was extended to kill.

    An ACH debit hits the checking account. Summing checking and savings into one
    "cash" figure and testing it against the buffer will happily overdraw checking
    while the savings balance sits untouched — the money is real, but it is not *there*,
    and moving it is a second ACH with its own delay.
    """
    d = decide(
        snapshot(
            accounts=(account("800.00"), savings("20000.00")),
            events=(),
            daily_discretionary_high=money("0.00"),
        )
    )

    assert d.action is Action.REFUSE
    assert d.projected_low_balance == money("800.00")  # checking only, not 20,800


def test_the_idle_savings_is_surfaced_rather_than_swept():
    """We won't move it — but staying silent about $20k earning nothing while the user
    pays 24% is its own kind of failure. Say it; don't act on it.
    """
    d = decide(
        snapshot(
            accounts=(account("800.00"), savings("20000.00")),
            events=(),
            daily_discretionary_high=money("0.00"),
        )
    )

    assert d.has(ReasonCode.IDLE_CASH_ELSEWHERE)


def test_sweeps_only_what_checking_can_cover():
    d = decide(
        snapshot(
            accounts=(account("1200.00"), savings("50000.00")),
            events=(),
            daily_discretionary_high=money("0.00"),
        )
    )

    # 1200 checking - 750 buffer - 400 obligation = 50. The savings is irrelevant.
    assert d.action is Action.SWEEP
    assert d.amount == money("50.00")


def test_a_pending_charge_on_savings_does_not_reduce_checking():
    d = decide(
        snapshot(
            accounts=(account("1200.00"), savings("5000.00")),
            events=(),
            pending=(
                PendingTransaction(
                    label="Transfer out", account_id="sav", amount=money("-4000.00")
                ),
            ),
            daily_discretionary_high=money("0.00"),
        )
    )

    assert d.projected_low_balance == money("1200.00")
    assert d.action is Action.SWEEP


def test_income_paid_into_savings_does_not_fund_a_checking_sweep():
    d = decide(
        snapshot(
            accounts=(account("800.00"), savings("100.00")),
            events=(paycheck(2, "6000.00", account_id="sav"),),
            daily_discretionary_high=money("0.00"),
        )
    )

    assert d.action is Action.REFUSE
    assert d.projected_low_balance == money("800.00")


def test_refuses_when_the_funding_account_is_not_a_checking_account():
    d = decide(
        snapshot(
            accounts=(savings("50000.00"),),
            funding_account_id="sav",
            events=(),
        )
    )

    assert d.action is Action.REFUSE
    assert d.has(ReasonCode.FUNDING_ACCOUNT_NOT_CHECKING)


def test_refuses_when_the_funding_account_is_missing_entirely():
    d = decide(snapshot(accounts=(account("50000.00"),), funding_account_id="nope", events=()))

    assert d.action is Action.REFUSE


def test_a_broken_savings_connection_does_not_block_a_checking_sweep():
    """Gates apply to the account the money actually leaves. A stale savings balance
    cannot overdraw checking, so refusing on it would be superstition, not safety.
    """
    d = decide(
        snapshot(
            accounts=(
                account("50000.00"),
                savings("100.00", connection=ConnectionState.LOGIN_REQUIRED, age=90),
            ),
            events=(),
        )
    )

    assert d.action is Action.SWEEP


# --- refusal: the feature ---------------------------------------------------------


def test_refuses_when_a_connection_needs_reauth():
    d = decide(snapshot(accounts=(account("50000.00", connection=ConnectionState.LOGIN_REQUIRED),)))

    assert d.action is Action.REFUSE
    assert d.has(ReasonCode.CONNECTION_UNHEALTHY)


def test_refuses_on_a_stale_balance():
    """A two-day-old balance is a guess. We do not move money against a guess."""
    d = decide(snapshot(accounts=(account("50000.00", age=3),)))

    assert d.action is Action.REFUSE
    assert d.has(ReasonCode.BALANCE_STALE)


def test_refuses_on_cold_start():
    """Day one, we know nothing. Saying so is better than inventing a number."""
    d = decide(snapshot(accounts=(account("50000.00"),), history_days=9))

    assert d.action is Action.REFUSE
    assert d.has(ReasonCode.INSUFFICIENT_HISTORY)


def test_refuses_when_income_is_too_volatile_to_forecast():
    """The gig worker's buffer is a rational hedge, not inefficiency.

    For this user the product is not merely unhelpful — sweeping their cash is worse
    advice than doing nothing. We decline to serve them rather than serve them badly.
    """
    d = decide(snapshot(accounts=(account("50000.00"),), income_variation=0.60))

    assert d.action is Action.REFUSE
    assert d.has(ReasonCode.INCOME_TOO_VARIABLE)


def test_refuses_during_a_user_blackout_window():
    d = decide(
        snapshot(accounts=(account("50000.00"),), policy=policy(blackout_dates=frozenset({TODAY})))
    )

    assert d.action is Action.REFUSE
    assert d.has(ReasonCode.BLACKOUT)


def test_refuses_while_a_previous_sweep_is_still_in_flight():
    """ACH is not instant. The balance we can see has not yet felt yesterday's sweep,
    and stacking a second one on top is how you overdraft someone with their own money.
    """
    d = decide(snapshot(accounts=(account("50000.00"),), sweeps_in_flight=money("200.00")))

    assert d.action is Action.REFUSE
    assert d.has(ReasonCode.SWEEP_IN_FLIGHT)


def test_refuses_rather_than_dipping_into_the_buffer():
    d = decide(
        snapshot(accounts=(account("800.00"),), events=(), daily_discretionary_high=money("0.00"))
    )

    assert d.action is Action.REFUSE
    assert d.projected_low_balance == money("800.00")


def test_protects_the_whole_obligation_not_merely_the_minimum():
    """The obligation is not surplus — and the minimum *understates* the obligation.

    This test used to reserve $180 and sweep $70. That was wrong, and wrong in the expensive
    direction. The minimum is what the **issuer** will accept; this household actually pays
    **$400** every month, which is precisely why they have idle cash to sweep in the first
    place. Reserving $180 and moving $70 leaves them $220 short of a payment they were always
    going to make.

    1000 in hand, 750 buffer => 250 apparently free. The real obligation is $400, so there is
    no surplus at all, and the honest answer is to refuse.
    """
    d = decide(
        snapshot(
            accounts=(account("1000.00"),),
            events=(),
            daily_discretionary_high=money("0.00"),
            policy=policy(max_sweep=money("300.00")),
        )
    )

    assert d.action is Action.REFUSE
    reserved = next(r for r in d.reasons if r.code is ReasonCode.NO_SURPLUS).params["reserved"]
    assert reserved == money("400.00")  # what they pay, not the $180 the issuer would take


def test_refuses_when_the_surplus_is_too_small_to_be_worth_the_ach_risk():
    d = decide(
        snapshot(
            accounts=(account("930.50"),),
            events=(),
            daily_discretionary_high=money("0.00"),
        )
    )

    # 930.50 - 750 - 180 = 0.50, below the minimum worth moving.
    assert d.action is Action.REFUSE


# --- APR is often missing, and we must not pretend otherwise ----------------------


def test_targets_the_highest_apr_card():
    d = decide(
        snapshot(
            accounts=(account("50000.00"),),
            events=(),
            portfolio=wallet(
                card(apr="0.1499", debt_id="low"),
                card(apr="0.2699", debt_id="high"),
            ),
        )
    )

    assert d.target_debt_id == "high"


def test_refuses_to_rank_when_aprs_are_unknown_and_there_is_a_choice():
    """Plaid frequently omits APR. Guessing which card to pay is not a small error —
    it silently destroys the entire value proposition while looking like it works.
    """
    d = decide(
        snapshot(
            accounts=(account("50000.00"),),
            events=(),
            portfolio=wallet(card(apr=None, debt_id="a"), card(apr=None, debt_id="b")),
        )
    )

    assert d.action is Action.REFUSE
    assert d.has(ReasonCode.APR_UNKNOWN)


def test_a_single_card_needs_no_apr_because_there_is_nothing_to_rank():
    d = decide(
        snapshot(
            accounts=(account("50000.00"),),
            events=(),
            portfolio=wallet(card(apr=None, debt_id="only")),
        )
    )

    assert d.action is Action.SWEEP
    assert d.target_debt_id == "only"


# --- what the sweep saved, and when we refuse to say ------------------------------


def test_a_sweep_says_what_it_saved():
    d = decide(snapshot())

    assert d.action is Action.SWEEP
    assert d.has(ReasonCode.INTEREST_AVOIDED)

    (saved,) = [r.params["amount"] for r in d.reasons if r.code is ReasonCode.INTEREST_AVOIDED]
    assert saved > money("0.00")


def test_a_sweep_against_a_card_with_no_apr_claims_nothing():
    """The one case where we move money and cannot say what it bought.

    Plaid does not return APR for many issuers. We still sweep — a single card needs no
    ranking — but there is no honest interest figure, so none is emitted. Silence, not a
    guess.
    """
    d = decide(
        snapshot(
            accounts=(account("50000.00"),),
            events=(),
            portfolio=wallet(card(apr=None, debt_id="only")),
        )
    )

    assert d.action is Action.SWEEP
    assert not d.has(ReasonCode.INTEREST_AVOIDED)


def test_a_household_underwater_on_their_own_payments_does_not_crash_the_decision():
    """Their payments don't cover their interest, so there is no payoff and no claim to make.

    The interest model raises on this — correctly; it is the most important fact about their
    finances. But the *decision* is unaffected: the sweep is still safe and still correct.
    We move the money and say nothing about what it saved.
    """
    underwater = card(debt_id="only", observed="10.00")  # $10/mo against ~$180/mo of interest

    d = decide(snapshot(accounts=(account("50000.00"),), events=(), portfolio=wallet(underwater)))

    assert d.action is Action.SWEEP
    assert not d.has(ReasonCode.INTEREST_AVOIDED)


def test_no_claim_until_we_have_seen_what_they_were_paying():
    """We never fall back to the card minimum as the counterfactual — that would flatter us."""
    d = decide(
        snapshot(
            accounts=(account("50000.00"),),
            events=(),
            portfolio=wallet(card(debt_id="only", observed=None)),
        )
    )

    assert d.action is Action.SWEEP
    assert not d.has(ReasonCode.INTEREST_AVOIDED)


def test_refuses_when_there_is_no_debt_left():
    """The happiest refusal. Also, per the strategy doc, the moment the customer
    stops being a customer — which is a business problem, not an engine problem.
    """
    d = decide(snapshot(accounts=(account("50000.00"),), events=(), portfolio=wallet()))

    assert d.action is Action.REFUSE
    assert d.has(ReasonCode.NO_DEBT)


# --- the decision must be explainable and reproducible ----------------------------


def test_every_decision_carries_its_reasons():
    for d in (decide(snapshot()), decide(snapshot(accounts=(account("100.00"),)))):
        assert d.reasons, "a decision with no explanation is not shippable"


# --- the minimum payment is reserved once, not twice --------------------------------


def test_a_detected_card_minimum_is_not_counted_twice():
    """The recurring-event detector will identify a card's minimum payment as a monthly
    obligation — it looks exactly like one. But `Debt` already carries that minimum and
    `decide()` reserves it. Subtracting it in the forecast too would take it twice.

    The direction is safe (we under-sweep), which is exactly why it would have gone
    unnoticed: the product would just quietly refuse more often than it should, forever.
    """
    without = decide(
        snapshot(
            accounts=(account("2000.00"),),
            events=(),
            daily_discretionary_high=money("0.00"),
            policy=policy(max_sweep=money("5000.00"), max_weekly_sweep=money("5000.00")),
        )
    )
    with_detected_minimum = decide(
        snapshot(
            accounts=(account("2000.00"),),
            events=(
                CashEvent(
                    label="VISA payment",
                    account_id="chk",
                    expected_date=TODAY + timedelta(days=20),
                    amount=money("-180.00"),
                    amount_low=money("-180.00"),
                    amount_high=money("-180.00"),
                    date_jitter_days=1,
                    confidence=0.99,
                    kind=EventKind.CARD_PAYMENT,
                ),
            ),
            daily_discretionary_high=money("0.00"),
            policy=policy(max_sweep=money("5000.00"), max_weekly_sweep=money("5000.00")),
        )
    )

    # 2000 - 750 buffer - 400 obligation. The obligation is reserved by decide(), full stop —
    # and a detected CARD_PAYMENT event does not subtract it a second time.
    assert without.amount == money("850.00")
    assert with_detected_minimum.amount == without.amount


def test_an_ordinary_obligation_is_still_counted():
    """Guard against the fix over-reaching: only CARD_PAYMENT is skipped."""
    d = decide(
        snapshot(
            accounts=(account("2000.00"),),
            events=(bill(20, "180.00", "Gym"),),
            daily_discretionary_high=money("0.00"),
            policy=policy(max_sweep=money("5000.00"), max_weekly_sweep=money("5000.00")),
        )
    )

    assert d.amount == money("670.00")  # 2000 - 750 - 400 obligation - 180 gym


def test_the_engine_never_reads_a_clock():
    """`today` is an input. Two runs of the same snapshot are byte-identical forever —
    which is what makes a sweep auditable after Plaid rewrites the history under us.
    """
    s = snapshot()

    assert decide(s) == decide(s)


@pytest.mark.parametrize("balance", ["0.00", "-25.00"])
def test_refuses_when_the_account_is_already_at_or_below_zero(balance):
    d = decide(snapshot(accounts=(account(balance),), events=()))

    assert d.action is Action.REFUSE


@pytest.mark.parametrize(
    ("balance", "expected"),
    [
        # 750 buffer + 400 obligation + 1.00 MIN_SWEEP. The boundary moved with the reserve:
        # it used to sit at 931 against a $180 minimum, and the $220 difference is the point.
        ("1151.00", Action.SWEEP),  # exactly MIN_SWEEP available
        ("1150.99", Action.REFUSE),  # a cent under
    ],
)
def test_the_min_sweep_boundary_is_inclusive(balance, expected):
    d = decide(
        snapshot(
            accounts=(account(balance),),
            events=(),
            daily_discretionary_high=money("0.00"),
        )
    )

    assert d.action is expected


# --- bad data must fail loudly, never quietly authorize a larger sweep -------------


def test_money_refuses_floats():
    """Decimal(2.675) is not 2.675 — it quantizes to 2.67. A silently wrong cent is
    the exact failure this type discipline exists to prevent.
    """
    with pytest.raises(TypeError, match="refuses floats"):
        money(2.675)


def test_an_outflow_bound_with_the_wrong_sign_is_rejected():
    """The sign bug that would otherwise understate a bill and cause an overdraft:
    amount=-1800 with amount_high=+2200 ("could be as much as $2,200") silently
    resolves to -1800 as the worst case, hiding $400 of exposure.
    """
    with pytest.raises(ValueError, match="different sign"):
        CashEvent(
            label="Utilities",
            account_id="chk",
            expected_date=TODAY,
            amount=money("-1800.00"),
            amount_low=money("-1800.00"),
            amount_high=money("2200.00"),
            date_jitter_days=1,
            confidence=0.9,
        )


def test_inverted_bounds_are_rejected():
    with pytest.raises(ValueError, match="larger in magnitude"):
        CashEvent(
            label="Rent",
            account_id="chk",
            expected_date=TODAY,
            amount=money("-1800.00"),
            amount_low=money("-2000.00"),
            amount_high=money("-1500.00"),
            date_jitter_days=1,
            confidence=0.9,
        )


def test_negative_jitter_is_rejected():
    """A negative jitter inverts the whole safety rule — obligations would be assumed
    to clear late and income to arrive early.
    """
    with pytest.raises(ValueError, match="date_jitter_days"):
        CashEvent(
            label="Rent",
            account_id="chk",
            expected_date=TODAY,
            amount=money("-1800.00"),
            amount_low=money("-1800.00"),
            amount_high=money("-1800.00"),
            date_jitter_days=-2,
            confidence=0.9,
        )


def test_a_negative_minimum_payment_is_rejected_rather_than_clamped():
    """A misread signed Liabilities field must not become an overdraft.

    Clamping it to zero would stop the overdraft but silently invent a $0 minimum —
    the engine guessing at money. It refuses to exist instead.
    """
    with pytest.raises(ValueError, match="minimum_payment"):
        Debt(
            debt_id="visa",
            balance=money("9000.00"),
            minimum_payment=money("-180.00"),
            minimum_due_date=TODAY + timedelta(days=20),
            apr=money("0.2399"),
        )


def test_an_implausible_apr_is_rejected():
    with pytest.raises(ValueError, match="apr"):
        Debt(
            debt_id="visa",
            balance=money("9000.00"),
            minimum_payment=money("180.00"),
            minimum_due_date=TODAY + timedelta(days=20),
            apr=money("24.99"),  # 2499%, not 24.99% — a units bug, caught at the door
        )


# --- the cadence: how often we are allowed to move money -------------------------
#
# prd.md §2 says we win this on the tail, not on expected value. Every sweep is an
# independent draw from that tail, so the number of draws is a risk control in its own
# right. These tests pin the one property that makes the cadence safe to reason about:
# it limits what we *do*, and never what we *know*.


def test_holds_when_the_last_sweep_was_too_recent():
    d = decide(snapshot(days_since_last_sweep=3, policy=policy(min_days_between_sweeps=7)))

    assert d.action is Action.REFUSE
    assert d.amount == ZERO
    assert ReasonCode.CADENCE_HOLD in {r.code for r in d.reasons}


def test_sweeps_again_once_the_spacing_has_elapsed():
    d = decide(snapshot(days_since_last_sweep=7, policy=policy(min_days_between_sweeps=7)))

    assert d.action is Action.SWEEP
    assert d.amount > ZERO


def test_a_household_we_have_never_swept_for_is_eligible_on_day_one():
    """None means "never swept", not "swept just now" — a new user must not wait a week."""
    d = decide(snapshot(days_since_last_sweep=None, policy=policy(min_days_between_sweeps=7)))

    assert d.action is Action.SWEEP


def test_a_cadence_hold_still_carries_its_projection_and_stays_gradeable():
    """The whole reason the hold sits *after* the forecast rather than in the blocking gates.

    engine/outcome.py:grade() raises on a decision with no projected_low_balance — a blocking
    refusal never ran the forecast, and grading one would feed the calibration distribution a
    zero that looks like a perfect prediction. If the cadence blocked up there, six days in
    seven would drop out of the record: the engine would still be wrong daily, but would no
    longer be *measured* daily, and strategy.md §3's error distribution is the only asset that
    compounds. Daily data, weekly money.
    """
    held = decide(snapshot(days_since_last_sweep=1, policy=policy(min_days_between_sweeps=7)))
    swept = decide(snapshot(days_since_last_sweep=None, policy=policy(min_days_between_sweeps=7)))

    assert held.action is Action.REFUSE
    assert held.projected_low_balance is not None
    # Same day, same household, same forecast — the cadence changed the action, not the view.
    assert held.projected_low_balance == swept.projected_low_balance
    assert ReasonCode.PROJECTION in {r.code for r in held.reasons}


def test_a_quiet_week_does_not_push_the_next_eligible_day_out():
    """The rule is spacing since the last *sweep*, not since the last *decision*.

    If a no-surplus day reset the clock, a household that ran thin for one day would be locked
    out for another full cadence — the engine punishing them for being poor that morning.
    """
    broke = snapshot(
        accounts=(account("760.00"),),  # under buffer + reserved: nothing to move
        days_since_last_sweep=9,
        policy=policy(min_days_between_sweeps=7),
    )
    d = decide(broke)

    assert d.action is Action.REFUSE
    codes = {r.code for r in d.reasons}
    assert ReasonCode.NO_SURPLUS in codes
    assert ReasonCode.CADENCE_HOLD not in codes  # eligible; there was simply no money


def test_zero_spacing_restores_the_daily_engine():
    """The cadence is a policy value, not a law of nature. 0 == the engine as originally shipped."""
    d = decide(snapshot(days_since_last_sweep=0, policy=policy(min_days_between_sweeps=0)))

    assert d.action is Action.SWEEP


def test_a_negative_spacing_is_rejected():
    with pytest.raises(ValueError, match="min_days_between_sweeps"):
        policy(min_days_between_sweeps=-1)


def test_a_negative_days_since_last_sweep_is_rejected():
    with pytest.raises(ValueError, match="days_since_last_sweep"):
        snapshot(days_since_last_sweep=-1)


class TestTheReserveTightens:
    """The plan's central safety claim, and the only reason this change may ship without
    calibration evidence:

        **The new reserve is never smaller than the old one — on every day of a full cycle.**

    The clause that matters is the last one. The first design of `obligation_in_horizon`
    reserved only the *closed* statement, which passes any spot-check taken in the first half
    of a cycle and silently drops to $0 for the last third of it. A sampled test would have
    reported green while the hole shipped. Walk the calendar.
    """

    def old_reserve(self, card: Card, today: date) -> Decimal:
        """What the engine used to reserve: the minimum, on a rolling next-due-date basis.

        `precompute.py` recomputed `minimum_due_date` fresh *every day* (`_next_due`), so it
        always pointed at the next due date and therefore reserved the minimum on essentially
        every day of the cycle. That rolling-forecast property is what made the crude reserve
        safe, and it is exactly what a "known fact, not a forecast" statement date throws away.
        """
        return card.minimum_payment

    def test_the_reserve_never_shrinks_on_any_day_of_a_full_cycle(self) -> None:
        """The household **pays** the statement, and that is the whole point.

        An earlier draft of this test held `statement_balance` at $2,000 forever. It passed with
        term 2 deleted — because a statement that is never paid keeps term 1 firing, which
        covers for the missing term. It was green and it was worthless.

        A real cycle: the statement closes, comes due, is **paid** (balance -> 0), and the next
        one accrues behind it. The window between "paid" and "next close" is where a one-term
        reserve reports $0 while the forecast has already skipped the payment event. Model the
        payment or this test proves nothing.
        """
        cycle = StatementCycle(close_day_of_month=20, grace_days=MIN_GRACE_DAYS)

        for offset in range(70):  # more than two full cycles, every single day
            today = date(2026, 1, 1) + timedelta(days=offset)

            # The **most recent** close on or before today — the same thing `derive_card` uses.
            # An earlier draft took "the close about a month back", which on a close day skips
            # straight over the statement that closed *today* and leaves it in neither term.
            # The test caught it, which is the entire reason it walks the calendar.
            last_close = cycle.close_on_or_after(today - timedelta(days=31))
            while cycle.close_on_or_after(last_close + timedelta(days=1)) <= today:
                last_close = cycle.close_on_or_after(last_close + timedelta(days=1))

            due = cycle.due_for(last_close)
            next_close = cycle.close_on_or_after(today + timedelta(days=1))

            # Once it is due, they pay it. That is what a household does, and it is the state
            # in which a closed-statement-only reserve holds back nothing at all.
            paid = today > due
            unbilled = money("900.00") if paid else money("300.00")

            card = Card(
                card_id="visa",
                apr=Decimal("0.2399"),
                cycle=cycle,
                statement_balance=ZERO if paid else money("2000.00"),
                statement_due_date=due,
                minimum_payment=money("180.00"),
                unbilled_balance=unbilled,
                next_close_date=next_close,
                behavior=PaymentBehavior.MINIMUM_ONLY,
                observed_monthly_payment=money("180.00"),
            )

            new = obligation_in_horizon(card, today + timedelta(days=HORIZON_DAYS))
            old = self.old_reserve(card, today)

            assert new >= old, (
                f"on {today} the new reserve ({new}) is SMALLER than the old one ({old}), "
                f"statement_paid={paid}. This is the hole: a reserve keyed only on the closed "
                "statement falls to zero once it is paid and before the next one closes."
            )

    def test_the_day_after_the_statement_is_paid_is_still_reserved(self) -> None:
        """The exact day the naive design broke.

        The closed statement has been paid, so it owes nothing. The next has not closed. A
        one-term reserve says $0 here — while the forecast has *already skipped* the card
        payment event, so nothing at all accounts for the money.
        """
        cycle = StatementCycle(close_day_of_month=20, grace_days=MIN_GRACE_DAYS)
        today = date(2026, 2, 11)  # the day after the Jan-20 statement came due

        card = Card(
            card_id="visa",
            apr=Decimal("0.2399"),
            cycle=cycle,
            statement_balance=ZERO,  # paid
            statement_due_date=date(2026, 2, 10),
            minimum_payment=money("180.00"),
            unbilled_balance=money("640.00"),  # and the next one is already accruing
            next_close_date=date(2026, 2, 20),
            behavior=PaymentBehavior.REVOLVER,
            observed_monthly_payment=money("450.00"),
        )

        reserved = obligation_in_horizon(card, today + timedelta(days=HORIZON_DAYS))

        assert reserved > ZERO, "term 2 is missing — this is the hole the review found"
        assert reserved == money("450.00")

    def test_a_transactor_reserves_the_whole_statement_not_the_minimum(self) -> None:
        """The $2,000 case. The reason the feature exists.

        A household charging $2,000/month to a card they clear in full has a $2,000 obligation
        and perhaps a $40 minimum. Reserve the minimum and $1,960 leaves checking on the 20th
        that we told them was theirs to sweep.
        """
        cycle = StatementCycle(close_day_of_month=20, grace_days=MIN_GRACE_DAYS)
        today = date(2026, 3, 1)

        transactor = Card(
            card_id="daily-driver",
            apr=Decimal("0.1899"),
            cycle=cycle,
            statement_balance=money("2000.00"),
            statement_due_date=date(2026, 3, 13),
            minimum_payment=money("40.00"),
            unbilled_balance=ZERO,
            next_close_date=date(2026, 3, 20),
            behavior=PaymentBehavior.TRANSACTOR,
            observed_monthly_payment=money("2000.00"),
        )

        reserved = obligation_in_horizon(transactor, today + timedelta(days=HORIZON_DAYS))
        assert reserved == money("2000.00")
        assert reserved != money("40.00")

    def test_a_transactor_is_reserved_against_but_never_swept_to(self) -> None:
        """Both halves of the [9.2] decision, in one place.

        They pay no interest — the grace period already does what our sweep claims to do. So a
        sweep to them is a prepayment, not a saving, and charging a share of it (prd §7.2)
        would be charging for nothing. But their statement still leaves checking, so it is
        still reserved.
        """
        target = card(apr="0.2399", debt_id="target", balance="9000.00")
        transactor = card(
            apr="0.2999",  # the highest APR — it would win the ranking outright
            debt_id="daily-driver",
            balance="2000.00",
            behavior=PaymentBehavior.TRANSACTOR,
            observed="2000.00",
        )

        d = decide(
            snapshot(
                accounts=(account("20000.00"),),
                events=(),
                daily_discretionary_high=money("0.00"),
                portfolio=wallet(target, transactor),
                policy=policy(max_sweep=money("5000.00"), max_weekly_sweep=money("5000.00")),
            )
        )

        assert d.action is Action.SWEEP
        assert d.target_debt_id == "target", "a transactor must never be the target"

        # ...and their $2,000 statement was reserved out of the surplus all the same.
        reserved = next(r for r in d.reasons if r.code is ReasonCode.PROJECTION).params["reserved"]
        assert reserved == money("2400.00")  # 2000 transactor + 400 revolver obligation

    def test_an_all_transactor_portfolio_refuses_rather_than_sweeping_for_the_look_of_it(
        self,
    ) -> None:
        d = decide(
            snapshot(
                accounts=(account("20000.00"),),
                events=(),
                portfolio=wallet(card(behavior=PaymentBehavior.TRANSACTOR, observed="2000.00")),
            )
        )

        assert d.action is Action.REFUSE
        assert d.has(ReasonCode.NO_INTEREST_TO_AVOID)


class TestCoverageBlocks:
    def test_an_unmatched_payment_blocks_no_matter_how_much_surplus_there_is(self) -> None:
        """Optimality within an incomplete portfolio is not optimality. It is an overdraft with
        a good explanation."""
        d = decide(
            snapshot(
                accounts=(account("50000.00"),),
                events=(),
                portfolio=CardPortfolio(
                    cards=(card(),),
                    coverage=CoverageState.UNMATCHED_PAYMENT,
                    unmatched_card_payments=(
                        UnmatchedPayment(
                            merchant="CHASE CARD SVC",
                            typical_amount=money("300.00"),
                            day_of_month=14,
                            months_observed=6,
                        ),
                    ),
                ),
            )
        )

        assert d.action is Action.REFUSE
        assert d.has(ReasonCode.CARD_COVERAGE_INCOMPLETE)

    def test_an_unattested_portfolio_blocks_too(self) -> None:
        """Chosen deliberately. It makes attestation a real onboarding gate rather than a
        checkbox — a household that has not confirmed its card list gets refusals, and that is
        the safe direction."""
        d = decide(
            snapshot(
                accounts=(account("50000.00"),),
                events=(),
                portfolio=CardPortfolio(cards=(card(),), coverage=CoverageState.UNATTESTED),
            )
        )

        assert d.action is Action.REFUSE
        assert d.has(ReasonCode.CARD_COVERAGE_INCOMPLETE)

    def test_a_card_we_have_not_watched_for_three_cycles_blocks(self) -> None:
        """We do not know what it will take out of checking. That figure sets the reserve *and*
        the interest claim, so a guess is load-bearing twice over."""
        d = decide(
            snapshot(
                accounts=(account("50000.00"),),
                events=(),
                portfolio=wallet(card(behavior=PaymentBehavior.UNKNOWN)),
            )
        )

        assert d.action is Action.REFUSE
        assert d.has(ReasonCode.CARD_BEHAVIOR_UNKNOWN)

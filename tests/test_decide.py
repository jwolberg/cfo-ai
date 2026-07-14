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

from engine.decide import MIN_SWEEP, decide
from engine.models import (
    ZERO,
    Account,
    AccountKind,
    Action,
    CashEvent,
    ConnectionState,
    Debt,
    EventKind,
    PendingTransaction,
    ReasonCode,
    Snapshot,
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
) -> Debt:
    return Debt(
        debt_id=debt_id,
        balance=money(balance),
        minimum_payment=money("180.00"),
        minimum_due_date=TODAY + timedelta(days=20),
        # Deliberately Decimal(), not money(): an APR is a rate, not a dollar amount, and
        # money() quantizes to cents — money("0.2399") is 0.24, a different card. Harmless
        # while APR is only ranked, wrong the moment it is multiplied by a balance.
        apr=Decimal(apr) if apr is not None else None,
        # What this household was already paying, well above the $180 minimum — which is
        # exactly why they have idle cash to sweep. This is the counterfactual the interest
        # claim is measured against (prd.md §5.1).
        observed_monthly_payment=money(observed) if observed is not None else None,
    )


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
        debts=(card(),),
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
            debts=(card(balance="42.00"),),
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

    # 1200 checking - 750 buffer - 180 minimum = 270. The savings is irrelevant.
    assert d.action is Action.SWEEP
    assert d.amount == money("270.00")


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


def test_protects_the_minimum_payment_before_any_extra_goes_out():
    """The minimum is not surplus. Sweeping it and then missing it would be an
    own-goal of the highest order: we would have caused the late fee we exist to prevent.
    """
    # 1000 in hand, 750 buffer => 250 apparently free. But a 180 minimum is due
    # inside the horizon, so only 70 is genuinely free — under the cap, and after
    # the minimum is reserved there is not enough left to be worth moving.
    d = decide(
        snapshot(
            accounts=(account("1000.00"),),
            events=(),
            daily_discretionary_high=money("0.00"),
            policy=policy(max_sweep=money("300.00")),
        )
    )

    assert d.action is Action.SWEEP
    assert d.amount == money("70.00")  # 1000 - 750 buffer - 180 minimum


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
            debts=(
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
            debts=(card(apr=None, debt_id="a"), card(apr=None, debt_id="b")),
        )
    )

    assert d.action is Action.REFUSE
    assert d.has(ReasonCode.APR_UNKNOWN)


def test_a_single_card_needs_no_apr_because_there_is_nothing_to_rank():
    d = decide(
        snapshot(
            accounts=(account("50000.00"),),
            events=(),
            debts=(card(apr=None, debt_id="only"),),
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
            debts=(card(apr=None, debt_id="only"),),
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

    d = decide(snapshot(accounts=(account("50000.00"),), events=(), debts=(underwater,)))

    assert d.action is Action.SWEEP
    assert not d.has(ReasonCode.INTEREST_AVOIDED)


def test_no_claim_until_we_have_seen_what_they_were_paying():
    """We never fall back to the card minimum as the counterfactual — that would flatter us."""
    d = decide(
        snapshot(
            accounts=(account("50000.00"),),
            events=(),
            debts=(card(debt_id="only", observed=None),),
        )
    )

    assert d.action is Action.SWEEP
    assert not d.has(ReasonCode.INTEREST_AVOIDED)


def test_refuses_when_there_is_no_debt_left():
    """The happiest refusal. Also, per the strategy doc, the moment the customer
    stops being a customer — which is a business problem, not an engine problem.
    """
    d = decide(snapshot(accounts=(account("50000.00"),), events=(), debts=()))

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
                    kind=EventKind.DEBT_MINIMUM,
                ),
            ),
            daily_discretionary_high=money("0.00"),
            policy=policy(max_sweep=money("5000.00"), max_weekly_sweep=money("5000.00")),
        )
    )

    # 2000 - 750 buffer - 180 minimum. The minimum is reserved by decide(), full stop.
    assert without.amount == money("1070.00")
    assert with_detected_minimum.amount == without.amount


def test_an_ordinary_obligation_is_still_counted():
    """Guard against the fix over-reaching: only DEBT_MINIMUM is skipped."""
    d = decide(
        snapshot(
            accounts=(account("2000.00"),),
            events=(bill(20, "180.00", "Gym"),),
            daily_discretionary_high=money("0.00"),
            policy=policy(max_sweep=money("5000.00"), max_weekly_sweep=money("5000.00")),
        )
    )

    assert d.amount == money("890.00")  # 2000 - 750 - 180 minimum - 180 gym


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
        ("931.00", Action.SWEEP),  # exactly MIN_SWEEP available
        ("930.99", Action.REFUSE),  # a cent under
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

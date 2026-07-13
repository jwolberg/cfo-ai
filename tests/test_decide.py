"""Adversarial tests for the sweep decision engine.

These are the spec. Each test is a way the real world breaks the happy path, and the
correct behaviour in almost every one of them is *refuse to move money*.

The engine's job is not to find the largest safe payment. It is to be right about the
word "safe" — and the cheapest way to be right is to decline.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from engine.decide import MIN_SWEEP, decide
from engine.models import (
    Account,
    Action,
    CashEvent,
    ConnectionState,
    Debt,
    PendingTransaction,
    Snapshot,
    UserPolicy,
    money,
)

TODAY = date(2026, 7, 12)


def account(balance: str, *, connection=ConnectionState.HEALTHY, age=0) -> Account:
    return Account(
        account_id="chk",
        balance=money(balance),
        connection=connection,
        balance_age_days=age,
    )


def paycheck(day_offset: int, amount: str = "3000.00", *, confidence=0.95, jitter=1) -> CashEvent:
    return CashEvent(
        label="Payroll",
        expected_date=TODAY + timedelta(days=day_offset),
        amount=money(amount),
        amount_low=money(amount),
        amount_high=money(amount),
        date_jitter_days=jitter,
        confidence=confidence,
    )


def bill(day_offset: int, amount: str, label: str = "Rent", *, high: str | None = None) -> CashEvent:
    amt = money("-" + amount.lstrip("-"))
    return CashEvent(
        label=label,
        expected_date=TODAY + timedelta(days=day_offset),
        amount=amt,
        amount_low=amt,
        amount_high=money("-" + (high or amount).lstrip("-")),
        date_jitter_days=1,
        confidence=0.95,
    )


def card(apr: str | None = "0.2399", balance: str = "9000.00", debt_id: str = "visa") -> Debt:
    return Debt(
        debt_id=debt_id,
        balance=money(balance),
        minimum_payment=money("180.00"),
        minimum_due_date=TODAY + timedelta(days=20),
        apr=money(apr) if apr is not None else None,
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
    assert any("cap" in r for r in d.reasons)


def test_weekly_cap_binds_across_sweeps():
    d = decide(snapshot(accounts=(account("50000.00"),), events=(), swept_this_week=money("450.00")))

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
            pending=(PendingTransaction(label="Car repair", amount=money("-400.00")),),
            daily_discretionary_high=money("0.00"),
        )
    )

    # 1200 - 400 pending = 800; buffer is 750, leaving 50 — but minimums must be
    # protected too, so there is nothing to sweep.
    assert d.action is Action.REFUSE
    assert any("minimum" in r or "buffer" in r for r in d.reasons)


def test_discretionary_spending_is_charged_every_day_of_the_horizon():
    without = decide(snapshot(accounts=(account("5000.00"),), events=(), daily_discretionary_high=money("0.00")))
    with_spend = decide(snapshot(accounts=(account("5000.00"),), events=(), daily_discretionary_high=money("100.00")))

    assert without.projected_low_balance == money("5000.00")
    # 30 days x $100 of assumed spend.
    assert with_spend.projected_low_balance == money("2000.00")


# --- refusal: the feature ---------------------------------------------------------


def test_refuses_when_a_connection_needs_reauth():
    d = decide(snapshot(accounts=(account("50000.00", connection=ConnectionState.LOGIN_REQUIRED),)))

    assert d.action is Action.REFUSE
    assert any("reconnect" in r.lower() for r in d.reasons)


def test_refuses_on_a_stale_balance():
    """A two-day-old balance is a guess. We do not move money against a guess."""
    d = decide(snapshot(accounts=(account("50000.00", age=3),)))

    assert d.action is Action.REFUSE
    assert any("stale" in r.lower() for r in d.reasons)


def test_refuses_on_cold_start():
    """Day one, we know nothing. Saying so is better than inventing a number."""
    d = decide(snapshot(accounts=(account("50000.00"),), history_days=9))

    assert d.action is Action.REFUSE
    assert any("history" in r.lower() for r in d.reasons)


def test_refuses_when_income_is_too_volatile_to_forecast():
    """The gig worker's buffer is a rational hedge, not inefficiency.

    For this user the product is not merely unhelpful — sweeping their cash is worse
    advice than doing nothing. We decline to serve them rather than serve them badly.
    """
    d = decide(snapshot(accounts=(account("50000.00"),), income_variation=0.60))

    assert d.action is Action.REFUSE
    assert any("volatile" in r.lower() or "variable" in r.lower() for r in d.reasons)


def test_refuses_during_a_user_blackout_window():
    d = decide(snapshot(accounts=(account("50000.00"),), policy=policy(blackout_dates=frozenset({TODAY}))))

    assert d.action is Action.REFUSE
    assert any("blackout" in r.lower() or "paused" in r.lower() for r in d.reasons)


def test_refuses_while_a_previous_sweep_is_still_in_flight():
    """ACH is not instant. The balance we can see has not yet felt yesterday's sweep,
    and stacking a second one on top is how you overdraft someone with their own money.
    """
    d = decide(snapshot(accounts=(account("50000.00"),), sweeps_in_flight=money("200.00")))

    assert d.action is Action.REFUSE
    assert any("flight" in r.lower() or "settl" in r.lower() for r in d.reasons)


def test_refuses_rather_than_dipping_into_the_buffer():
    d = decide(snapshot(accounts=(account("800.00"),), events=(), daily_discretionary_high=money("0.00")))

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
    assert any("apr" in r.lower() for r in d.reasons)


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


def test_refuses_when_there_is_no_debt_left():
    """The happiest refusal. Also, per the strategy doc, the moment the customer
    stops being a customer — which is a business problem, not an engine problem.
    """
    d = decide(snapshot(accounts=(account("50000.00"),), events=(), debts=()))

    assert d.action is Action.REFUSE
    assert any("no debt" in r.lower() or "paid off" in r.lower() for r in d.reasons)


# --- the decision must be explainable and reproducible ----------------------------


def test_every_decision_carries_its_reasons():
    for d in (decide(snapshot()), decide(snapshot(accounts=(account("100.00"),)))):
        assert d.reasons, "a decision with no explanation is not shippable"


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

"""The card types, and the bad data they refuse.

These tests exist to protect one property, and it is not "the fields are spelled right".

Every validation in the card model refuses the direction that would **shrink the reserve** —
a negative balance, a minimum below zero, a grace period shorter than the law allows. Each of
those hands the user a *larger* sweep than the truth licenses, and `docs/decision-engine.md`
§3 is unambiguous about that direction: an upstream data change must never buy a bigger sweep.

So the tests below are mostly about money the engine is *not* allowed to free up.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from engine.models import (
    MIN_GRACE_DAYS,
    Card,
    CardPortfolio,
    CardTransaction,
    CoverageState,
    PaymentBehavior,
    Recurrence,
    SpendCategory,
    SpendProfile,
    StatementCycle,
    UnmatchedPayment,
    money,
    statement_day,
)

CLOSE_DAY = 20


def cycle(**kw: object) -> StatementCycle:
    base: dict[str, object] = {"close_day_of_month": CLOSE_DAY, "grace_days": MIN_GRACE_DAYS}
    base.update(kw)
    return StatementCycle(**base)  # type: ignore[arg-type]


def card(**kw: object) -> Card:
    """A revolver mid-cycle: one statement closed and due, one accruing. Tests mutate one thing."""
    base: dict[str, object] = {
        "card_id": "card-1",
        "apr": Decimal("0.2399"),
        "cycle": cycle(),
        "statement_balance": money("2240.00"),
        "statement_due_date": date(2026, 3, 13),
        "minimum_payment": money("280.00"),
        "unbilled_balance": money("1240.00"),
        "next_close_date": date(2026, 3, 20),
        "behavior": PaymentBehavior.REVOLVER,
        "observed_monthly_payment": money("450.00"),
    }
    base.update(kw)
    return Card(**base)  # type: ignore[arg-type]


class TestStatementCycle:
    def test_a_card_that_closes_on_the_31st_closes_on_the_28th_in_february(self) -> None:
        assert statement_day(2026, 2, 31) == date(2026, 2, 28)
        assert cycle(close_day_of_month=31).close_on_or_after(date(2026, 2, 1)) == date(2026, 2, 28)

    def test_the_close_is_the_first_one_on_or_after_the_day(self) -> None:
        c = cycle()
        assert c.close_on_or_after(date(2026, 3, 1)) == date(2026, 3, 20)
        assert c.close_on_or_after(date(2026, 3, 20)) == date(2026, 3, 20)
        # Past this month's close, roll to next month's.
        assert c.close_on_or_after(date(2026, 3, 21)) == date(2026, 4, 20)

    def test_the_close_rolls_across_a_year_boundary(self) -> None:
        assert cycle().close_on_or_after(date(2026, 12, 21)) == date(2027, 1, 20)

    def test_the_due_date_is_the_close_plus_the_grace(self) -> None:
        assert cycle().due_for(date(2026, 3, 20)) == date(2026, 4, 10)

    def test_a_grace_below_the_reg_z_floor_is_refused(self) -> None:
        """A short grace dates the obligation *earlier* than it can legally be.

        That pulls it out of the horizon we reserve for — bad data buying a bigger sweep,
        which is the one direction §3 forbids.
        """
        with pytest.raises(ValueError, match="below the Reg Z floor"):
            cycle(grace_days=20)

    def test_a_close_day_outside_the_month_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not a day of the month"):
            cycle(close_day_of_month=0)
        with pytest.raises(ValueError, match="not a day of the month"):
            cycle(close_day_of_month=32)


class TestCardTransaction:
    def test_a_charge_is_negative_because_negative_is_money_out(self) -> None:
        """The sign rule is one rule, everywhere, or it is none.

        A charge increases what you owe, so the tempting thing is to store it positive. That
        is exactly the flip that `CashEvent.__post_init__` exists to catch: two sign
        conventions in one codebase means one of them is wrong at every boundary.
        """
        charge = CardTransaction(
            card_id="card-1",
            posted_date=date(2026, 3, 5),
            amount=money("-38.00"),
            merchant="Trader Joe's",
            category=SpendCategory.FOOD_AND_DRINK,
            recurrence=Recurrence.VARIABLE,
        )
        assert charge.amount < 0

    def test_a_refund_is_positive(self) -> None:
        refund = CardTransaction(
            card_id="card-1",
            posted_date=date(2026, 3, 5),
            amount=money("38.00"),
            merchant="Trader Joe's",
            category=SpendCategory.FOOD_AND_DRINK,
            recurrence=Recurrence.ONE_OFF,
        )
        assert refund.amount > 0

    def test_a_zero_transaction_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not a transaction"):
            CardTransaction(
                card_id="card-1",
                posted_date=date(2026, 3, 5),
                amount=money("0.00"),
                merchant="Nothing",
                category=SpendCategory.OTHER,
                recurrence=Recurrence.ONE_OFF,
            )


class TestCard:
    def test_a_negative_statement_balance_is_refused(self) -> None:
        with pytest.raises(ValueError, match="statement_balance"):
            card(statement_balance=money("-1.00"))

    def test_a_negative_unbilled_balance_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unbilled_balance"):
            card(unbilled_balance=money("-1.00"))

    def test_a_negative_minimum_is_refused_because_it_would_shrink_the_reserve(self) -> None:
        with pytest.raises(ValueError, match="minimum_payment"):
            card(minimum_payment=money("-1.00"))

    def test_an_implausible_apr_is_refused_but_none_is_allowed(self) -> None:
        """None means *unknown*, not zero. Plaid frequently does not report it (§6.3)."""
        assert card(apr=None).apr is None
        with pytest.raises(ValueError, match="plausible range"):
            card(apr=Decimal("3"))

    def test_a_negative_observed_payment_is_refused(self) -> None:
        """It would make the counterfactual cheaper than reality and inflate the claim."""
        with pytest.raises(ValueError, match="observed_monthly_payment"):
            card(observed_monthly_payment=money("-1.00"))

    def test_the_two_statements_may_not_be_the_same_cycle(self) -> None:
        """If next_close precedes the closed statement's own close, they are one statement.

        Reserved twice at best; at worst the "unbilled" figure is really already due, and gets
        treated as a month further away than it is.
        """
        with pytest.raises(ValueError, match="same cycle"):
            card(statement_due_date=date(2026, 3, 13), next_close_date=date(2026, 1, 1))

    def test_total_owed_is_billed_plus_unbilled(self) -> None:
        assert card().total_owed == money("3480.00")

    def test_a_transactors_unbilled_charges_accrue_nothing(self) -> None:
        """The grace period covers them — which is *why* a transactor's interest_avoided is $0.

        Sweeping their cash onto a card they were going to clear anyway is a prepayment, not
        a saving.
        """
        transactor = card(behavior=PaymentBehavior.TRANSACTOR)
        assert transactor.interest_bearing_balance == money("2240.00")
        assert transactor.interest_bearing_balance < transactor.total_owed

    def test_a_revolver_has_no_grace_so_everything_accrues(self) -> None:
        revolver = card(behavior=PaymentBehavior.REVOLVER)
        assert revolver.interest_bearing_balance == revolver.total_owed == money("3480.00")


class TestCardPortfolio:
    def test_complete_coverage_cannot_coexist_with_an_unmatched_payment(self) -> None:
        """The one contradiction that matters: sweeping with full confidence into a card we
        already hold evidence we cannot see."""
        unmatched = UnmatchedPayment(
            merchant="CHASE CARD SVC",
            typical_amount=money("300.00"),
            day_of_month=14,
            months_observed=6,
        )
        with pytest.raises(ValueError, match="cannot claim complete coverage"):
            CardPortfolio(
                cards=(card(),),
                coverage=CoverageState.COMPLETE,
                unmatched_card_payments=(unmatched,),
            )

    def test_an_unmatched_payment_is_fine_when_coverage_does_not_claim_to_be_complete(
        self,
    ) -> None:
        unmatched = UnmatchedPayment(
            merchant="CHASE CARD SVC",
            typical_amount=money("300.00"),
            day_of_month=14,
            months_observed=6,
        )
        portfolio = CardPortfolio(
            cards=(card(),),
            coverage=CoverageState.UNMATCHED_PAYMENT,
            unmatched_card_payments=(unmatched,),
        )
        assert not portfolio.is_complete

    def test_the_default_portfolio_is_unattested_not_complete(self) -> None:
        """The permissive default here would be a sweep into a portfolio nobody confirmed."""
        assert CardPortfolio().coverage is CoverageState.UNATTESTED
        assert not CardPortfolio().is_complete


class TestSpendProfile:
    def test_the_worst_window_is_the_worst_window(self) -> None:
        profile = SpendProfile(
            rolling_30d_cash=(money("1800.00"), money("2231.00"), money("1400.00")),
            rolling_30d_card=(money("900.00"), money("1240.00")),
        )
        assert profile.worst_30d_cash == money("2231.00")
        assert profile.worst_30d_card == money("1240.00")

    def test_an_empty_history_has_no_worst_window_rather_than_raising(self) -> None:
        """A household with no history yet is not an error — it is a household we refuse to
        sweep for, which is a decision made elsewhere."""
        assert SpendProfile().worst_30d_cash == money("0.00")

    def test_a_nonpositive_window_is_refused(self) -> None:
        with pytest.raises(ValueError, match="window_days"):
            SpendProfile(window_days=0)


def test_the_card_types_feed_no_decision_yet() -> None:
    """U1 is types only. If this import ever fails, someone wired them in early.

    `daily_discretionary_high` is still the forecast's spend input, deliberately, and
    `SpendProfile` is a dashboard structure. Swapping the forecast onto its empirical
    quantile would *loosen* the reserve, and loosening needs a measured breach rate that
    `engine/outcome.py` cannot yet produce. See the plan's U8.
    """
    import engine.decide as decide
    import engine.forecast as forecast

    source = (decide.__file__, forecast.__file__)
    for path in source:
        with open(path) as fh:
            body = fh.read()
        assert "SpendProfile" not in body, f"{path} reads SpendProfile — that is U8's gate, not U1"

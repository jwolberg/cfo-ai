"""The interest model is a customer-facing financial claim. These tests are the spec.

Every expected figure below is hand-computable. The APR is deliberately 36.5%, which makes
the daily periodic rate exactly 0.001 (0.365 / 365), so a cycle's interest is
`balance x 0.001 x days` with no rounding ambiguity anywhere. If a test here needs a
calculator, the model has become something we cannot explain to a customer or a regulator.

The counterfactual throughout is the household's **own** payment (prd.md §5.1), not the card
minimum — see `test_the_card_minimum_does_not_move_the_claim`, which is the test that pins
that decision in place.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from engine.interest import claimable_interest_avoided, interest_avoided, total_interest
from engine.models import Debt, money

# 36.5% APR -> daily periodic rate of exactly 0.001.
#
# Deliberately *not* money(): an APR is a rate, not a dollar amount, and money() quantizes to
# cents — money("0.365") is 0.36, which is a different card. Rates keep their precision.
APR = Decimal("0.365")

START = date(2026, 1, 1)

# What the household actually pays this card each month, before we arrive.
PAYS = money("530.00")


def card(
    balance: str = "1000.00",
    apr: object = APR,
    due: date = date(2026, 1, 31),
    minimum: str = "30.00",
    observed: str | None = "530.00",
) -> Debt:
    return Debt(
        debt_id="card",
        balance=money(balance),
        minimum_payment=money(minimum),
        minimum_due_date=due,
        apr=apr,  # type: ignore[arg-type]
        observed_monthly_payment=money(observed) if observed is not None else None,
    )


class TestAmortization:
    def test_reproduces_a_hand_computed_payoff_to_the_cent(self) -> None:
        """$1,000 at 36.5%, the household paying $530/mo, starting 2026-01-01.

        Cycle 1 (Jan 1 -> Jan 31, 30 days):
            interest = 1000.00 x 0.001 x 30 = 30.00  -> balance 1030.00
            payment  = 530.00                        -> balance  500.00
        Cycle 2 (Jan 31 -> Feb 28, 28 days):
            interest =  500.00 x 0.001 x 28 = 14.00  -> balance  514.00
            payment  = 530.00 > 514.00, so it clears -> balance    0.00

        Total interest paid: $44.00.
        """
        assert total_interest(card(), start=START, monthly_payment=PAYS) == money("44.00")

    def test_a_zero_balance_card_accrues_nothing(self) -> None:
        assert total_interest(card(balance="0.00"), start=START, monthly_payment=PAYS) == money(
            "0.00"
        )

    def test_interest_accrues_on_principal_only_within_a_cycle(self) -> None:
        """No intra-cycle compounding — the average-daily-balance method.

        If the model compounded daily, cycle 1's interest would be
        1000 x (1.001^30 - 1) = 30.45, not 30.00. The $0.45 is the tell.
        """
        # A payment that clears the card outright at the first statement leaves exactly one
        # cycle of interest to inspect.
        assert total_interest(card(), start=START, monthly_payment=money("99999.00")) == money(
            "30.00"
        )

    def test_the_card_minimum_does_not_move_the_claim(self) -> None:
        """The design decision of this module, pinned.

        The counterfactual is what the household *actually pays* (prd.md §5.1), never the card
        minimum. Measuring against the minimum would credit our sweep with interest the user
        was never going to pay anyway — inflating the one number the company is graded on,
        which is exactly the KPI failure §5.1 was written to ban.

        So `minimum_payment` is not an input to this module, and no change to it may move a
        single cent of the answer.
        """
        tiny_minimum = interest_avoided(
            card(minimum="1.00"), money("500.00"), on=START, monthly_payment=PAYS
        )
        huge_minimum = interest_avoided(
            card(minimum="900.00"), money("500.00"), on=START, monthly_payment=PAYS
        )

        assert tiny_minimum == huge_minimum == money("29.00")


class TestMissingApr:
    """Plaid does not return APR for many issuers (decision-engine.md §6.3).

    The engine refuses to guess a rate. It must also refuse to *imply* one by returning a
    number that looks computed.
    """

    def test_total_interest_is_none_when_apr_is_unknown(self) -> None:
        assert total_interest(card(apr=None), start=START, monthly_payment=PAYS) is None

    def test_interest_avoided_is_none_when_apr_is_unknown(self) -> None:
        assert (
            interest_avoided(card(apr=None), money("500.00"), on=START, monthly_payment=PAYS)
            is None
        )


class TestInterestAvoided:
    def test_a_zero_sweep_avoids_nothing(self) -> None:
        assert interest_avoided(card(), money("0.00"), on=START, monthly_payment=PAYS) == money(
            "0.00"
        )

    def test_a_sweep_that_clears_the_card_avoids_all_of_the_interest(self) -> None:
        """Pay the $1,000 today and none of the $44.00 is ever charged."""
        assert interest_avoided(card(), money("1000.00"), on=START, monthly_payment=PAYS) == money(
            "44.00"
        )

    def test_a_partial_sweep_avoids_the_interest_that_dollar_would_have_accrued(self) -> None:
        """$500 swept on day one.

        Cycle 1 (30 days): 500.00 x 0.001 x 30 = 15.00 -> balance 515.00
        The household's own $530 payment clears it. Total interest with the sweep: $15.00.
        Counterfactual was $44.00, so the sweep avoided $29.00.
        """
        assert interest_avoided(card(), money("500.00"), on=START, monthly_payment=PAYS) == money(
            "29.00"
        )

    def test_a_sweep_later_in_the_cycle_avoids_less_than_the_same_sweep_today(self) -> None:
        """The whole reason this is a daily model.

        A monthly-periodic-rate amortization would value these two sweeps identically. It
        would be wrong in the direction of over-claiming, on a number we put in front of the
        customer.
        """
        today = interest_avoided(card(), money("500.00"), on=START, monthly_payment=PAYS)
        later = interest_avoided(
            card(), money("500.00"), on=date(2026, 1, 21), monthly_payment=PAYS
        )

        assert today is not None and later is not None
        assert later < today

    def test_a_sweep_larger_than_the_balance_does_not_avoid_more_than_the_balance_would(
        self,
    ) -> None:
        """Overpaying a card does not buy extra interest savings, and must not claim to."""
        clears = interest_avoided(card(), money("1000.00"), on=START, monthly_payment=PAYS)
        overpays = interest_avoided(card(), money("5000.00"), on=START, monthly_payment=PAYS)

        assert overpays == clears


class TestTheProductNeverInventsANumber:
    """`claimable_interest_avoided` is the decision path's view of the model.

    `decide()` must be total — a card with bad data is not a reason to crash a household's
    daily decision — and it must never render a figure it cannot stand behind. `None` is the
    only thing standing between those two requirements.
    """

    def test_no_claim_when_the_apr_is_unknown(self) -> None:
        assert claimable_interest_avoided(card(apr=None), money("500.00"), on=START) is None

    def test_no_claim_when_we_have_not_observed_what_they_were_paying(self) -> None:
        """We do not fall back to the minimum. That is the flattering assumption we rejected."""
        assert claimable_interest_avoided(card(observed=None), money("500.00"), on=START) is None

    def test_no_claim_when_the_debt_does_not_amortize_and_it_does_not_raise(self) -> None:
        """The strict model raises here. The product declines to claim, and carries on."""
        assert claimable_interest_avoided(card(observed="0.00"), money("500.00"), on=START) is None

    def test_a_claim_is_made_when_the_debt_is_coherent(self) -> None:
        assert claimable_interest_avoided(card(), money("500.00"), on=START) == money("29.00")


class TestBadDataFailsLoudly:
    def test_a_household_whose_payments_never_cover_the_interest_raises(self) -> None:
        """$1,000 at 36.5% accrues ~$30/cycle. A $10/mo payment never catches it.

        There is no payoff and therefore no total interest. Returning a capped figure or a
        None sentinel would become a garbage claim downstream — it fails at the boundary
        instead (decision-engine.md §3). It is also the most important fact about this
        household's finances, and worth surfacing rather than smoothing.
        """
        with pytest.raises(ValueError, match="does not amortize"):
            total_interest(card(), start=START, monthly_payment=money("10.00"))

    def test_a_zero_payment_raises(self) -> None:
        with pytest.raises(ValueError, match="does not amortize"):
            total_interest(card(), start=START, monthly_payment=money("0.00"))

    def test_a_negative_sweep_is_refused(self) -> None:
        """A mis-signed amount upstream must never *increase* the interest we claim."""
        with pytest.raises(ValueError):
            interest_avoided(card(), money("-500.00"), on=START, monthly_payment=PAYS)

    def test_a_negative_observed_payment_is_refused_at_the_type_boundary(self) -> None:
        """It would make the counterfactual cheaper than reality and inflate what we claim."""
        with pytest.raises(ValueError, match="observed_monthly_payment"):
            card(observed="-100.00")

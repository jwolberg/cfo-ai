"""The projected-balance trajectory (ticket 0064).

`conservative_low_balance`'s exact values are already pinned across the engine suites; this file
covers the new public curve `balance_trajectory` and the one property the chart depends on: the low
the decision is made on is a point on the very curve the chart draws. One walk, no drift.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend.archetypes import ARCHETYPES
from backend.precompute import (
    DEMO_POLICY,
    SEED,
    SERVED_DAYS,
    SPEND_QUANTILE,
    WARMUP_DAYS,
    WINDOW_START,
    generate,
    walk,
)
from engine.forecast import HORIZON_DAYS, balance_trajectory, conservative_low_balance


def _walk_days(archetype: str):
    spec = ARCHETYPES[archetype]
    total = WARMUP_DAYS + SERVED_DAYS
    history = generate(spec, start=WINDOW_START, days=total, seed=SEED)
    return list(walk(history, spec, WINDOW_START, total, DEMO_POLICY, SPEND_QUANTILE))


def _snapshot(archetype: str, day: date):
    return next(w.snapshot for w in _walk_days(archetype) if w.day == day)


class TestTheTrajectory:
    def test_it_is_the_whole_horizon_plus_a_now_anchor(self) -> None:
        s = _snapshot("monthly_thin", date(2026, 5, 21))
        traj = balance_trajectory(s)
        # a "now" anchor at today, then one point per horizon day (today..today+HORIZON)
        assert len(traj) == HORIZON_DAYS + 2
        assert traj[0][0] == s.today
        assert traj[-1][0] == s.today + timedelta(days=HORIZON_DAYS)
        assert [d for d, _ in traj] == [
            s.today,
            *(s.today + timedelta(days=i) for i in range(HORIZON_DAYS + 1)),
        ]

    def test_the_low_is_a_point_on_the_curve_the_chart_draws(self) -> None:
        """The property the whole ticket rests on: the decision's low and the chart's low are the
        same number, because they come from the same walk."""
        s = _snapshot("monthly_thin", date(2026, 5, 21))
        low, low_day = conservative_low_balance(s)
        by_day = dict(balance_trajectory(s))
        assert low_day in by_day
        assert by_day[low_day] == low
        assert low == min(b for _, b in balance_trajectory(s))


class TestItNeverDisagreesWithTheDecision:
    @pytest.mark.parametrize(
        "archetype", ["demo_biweekly", "semimonthly_portfolio", "monthly_thin"]
    )
    def test_the_reported_low_is_the_curves_minimum_every_served_day(self, archetype: str) -> None:
        """Across every graded day of three cadences: the low the engine refuses/sweeps on equals
        the minimum of the curve it will plot. If these ever drift, a customer sees a chart whose
        lowest point is not the number the decision quoted."""
        for w in _walk_days(archetype):
            if w.offset < WARMUP_DAYS:
                continue
            low, _low_day = conservative_low_balance(w.snapshot)
            assert low == min(b for _, b in balance_trajectory(w.snapshot)), w.day


class TestTheHouseholdWhoseIncomeStopped:
    """Archetype E — the one case where the *balance* and the *forecast* disagree.

    Every other archetype holds a stable W2 constant and varies the calendar or the cards, so all of
    them exercise a household whose forecast is fundamentally sound. This one does not, and it is
    where being wrong is most expensive: for weeks after the money stops the balance still looks
    healthy, so anything sweeping on the balance keeps sweeping toward the overdraft.

    These assertions are about the *shape* of the outcome, deliberately — not exact amounts, which
    would break on any reseed or engine tweak and would be testing the fixture rather than the
    behaviour.
    """

    STOP = date(2026, 4, 10)  # `income_stopped.payroll.last_payday`

    def _served(self):
        return [w for w in _walk_days("income_stopped") if w.day >= date(2026, 3, 2)]

    def test_the_income_actually_stops_inside_the_served_window(self) -> None:
        # If the stop fell in the warmup, nothing would change across the served feed and the
        # archetype would be indistinguishable from a household that was simply never paid.
        served = self._served()
        assert served[0].day < self.STOP < served[-1].day

    def test_it_sweeps_while_paid_and_never_again_after(self) -> None:
        served = self._served()
        paid = [w for w in served if w.day <= self.STOP]
        unpaid = [w for w in served if w.day > self.STOP]

        assert any(w.decision.action.value == "sweep" for w in paid), (
            "the engine must be seen working before the income stops, or the feed shows no contrast"
        )
        assert not any(w.decision.action.value == "sweep" for w in unpaid), (
            "sweeping after the paychecks stopped is the exact failure this archetype prices"
        )

    def test_the_balance_still_looks_healthy_when_the_money_stops(self) -> None:
        # The heart of it: on the last payday the household is *well* above their buffer, so
        # nothing about the balance alone justifies standing down. Only the forecast does.
        at_stop = next(w for w in self._served() if w.day == self.STOP)
        checking = at_stop.snapshot.accounts[0].balance
        assert checking > at_stop.snapshot.policy.buffer_floor * 3

    def test_it_never_overdrafts_the_household(self) -> None:
        assert all(w.snapshot.accounts[0].balance >= 0 for w in self._served())

    def test_debt_remains_to_pay_when_the_income_stops(self) -> None:
        # Guards the card balance choice (see `archetypes.py`): if the card is nearly retired by the
        # time income stops, the hero celebrates a near-finished paydown while the feed refuses for
        # want of surplus, and the screen contradicts itself.
        at_stop = next(w for w in self._served() if w.day == self.STOP)
        owed = sum(c.total_owed for c in at_stop.snapshot.portfolio.cards)
        assert owed > 2000, f"only ${owed} owed at the stop — too little for the story to read"


class TestEveryArchetypeAmortizes:
    """A counterfactual payment must comfortably exceed the card's monthly interest.

    Not a style rule. `engine/interest.py` prices a sweep against *"what this card costs if the
    household keeps paying what they pay"*, so a card whose payment barely covers its interest has a
    payoff horizon measured in decades and an avoided-interest figure to match. `income_stopped`
    shipped that way for an afternoon: $250/mo against ~$239.90 of interest — $10 to principal — and
    one $1,600 sweep claimed $10,590 of avoided interest on a $12,000 card.

    **The guard in `claimable_interest_avoided` does not catch this.** It returns `None` only when
    payments fail to cover interest *at all*; a card amortizing by a dollar a month sails past it
    and returns a number that is arithmetically correct and completely useless. Nothing about the
    spec looks wrong, and the symptom only shows up in a headline stat. So it is asserted here.
    """

    def test_payment_sends_a_real_share_to_principal(self) -> None:
        from decimal import Decimal

        for name, spec in ARCHETYPES.items():
            for card in spec.cards:
                monthly_interest = Decimal(card.balance) * Decimal(card.apr) / 12
                to_principal = Decimal(card.payment) - monthly_interest
                # A quarter of the payment is a low bar deliberately: this is a smoke alarm for
                # specs that do not amortize in practice, not a design opinion about the ratio.
                assert to_principal > Decimal(card.payment) * Decimal("0.25"), (
                    f"{name}/{card.card_id}: ${card.payment} payment against "
                    f"${monthly_interest:.2f} monthly interest sends only ${to_principal:.2f} to "
                    "principal — interest-avoided will be enormous and meaningless"
                )

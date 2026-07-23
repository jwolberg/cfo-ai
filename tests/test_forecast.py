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

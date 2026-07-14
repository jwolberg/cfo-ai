"""How wrong is the engine's discretionary-spend assumption? Measured, not asserted.

`forecast.py` subtracts `daily_discretionary_high` — a **p90 of DAILY spend** — on every one
of the 30 horizon days. Compounding a per-day quantile is not a horizon quantile: variance
grows with the square root of time, and this model grows it linearly.

These are **characterization tests**. They pin the *current* behaviour and its cost, against
households whose real 30-day spend we can simply enumerate. They are expected to fail if and
when the spend model is fixed — that is the point. A failure here means "the thing you set out
to change has changed", and the numbers below should then be re-derived, not deleted.

See docs/learnings/2026-07-13-the-spend-model-over-reserves.md.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from engine.forecast import HORIZON_DAYS
from engine.models import money
from sim.household import (
    BillSpec,
    CardSpec,
    HouseholdSpec,
    PayCadence,
    PayrollSpec,
    SpendSpec,
    generate,
)

START = date(2024, 1, 1)
DAYS = 1095  # three years — enough 30-day windows to speak about a tail at all


def quantile(xs: list[float], q: float) -> float:
    ordered = sorted(xs)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def household(spend: SpendSpec, seed: int):
    spec = HouseholdSpec(
        opening_balance=money("4000.00"),
        payroll=PayrollSpec(
            net_pay=money("2600.00"),
            cadence=PayCadence.BIWEEKLY,
            first_payday=START,
            variation=Decimal("0.02"),
        ),
        bills=(BillSpec(label="rent", day_of_month=1, mean=money("1800.00")),),
        spend=spend,
        cards=(
            CardSpec(
                balance=money("9000.00"),
                apr=Decimal("0.2399"),
                minimum_payment=money("180.00"),
                payment=money("400.00"),
            ),
        ),
    )
    return generate(spec, start=START, days=DAYS, seed=seed)


PROFILES = {
    # (spend spec, seed) — three shapes of real household.
    "typical": (SpendSpec(zero_day_probability=0.25, median=money("38.00"), log_sigma=0.9), 11),
    "high_variance": (
        SpendSpec(zero_day_probability=0.30, median=money("35.00"), log_sigma=1.4),
        12,
    ),
    "steady": (SpendSpec(zero_day_probability=0.10, median=money("50.00"), log_sigma=0.4), 13),
}


def measure(name: str) -> tuple[float, float, float]:
    """Returns (what the engine assumes, the real p99 30-day spend, the worst ever seen)."""
    spend_spec, seed = PROFILES[name]
    daily = [float(x) for x in household(spend_spec, seed).discretionary_series()]

    engine_assumes = HORIZON_DAYS * quantile(daily, 0.90)

    windows = [sum(daily[i : i + HORIZON_DAYS]) for i in range(len(daily) - HORIZON_DAYS + 1)]

    return engine_assumes, quantile(windows, 0.99), max(windows)


@pytest.mark.parametrize("profile", list(PROFILES))
def test_the_engine_reserves_more_than_the_household_has_ever_spent(profile: str) -> None:
    """The finding, in one assertion.

    For every household shape, `30 x p90_daily` exceeds the **worst 30-day stretch in three
    years of that household's own life**. It is not a conservative estimate of a bad month; it
    is a month worse than any they have ever had.

    This is the safe direction — nobody is overdrawn — which is exactly why it would have
    survived. Nothing throws, no alert fires; the product simply refuses more often than it
    should, quietly, forever (decision-engine.md §3.1).
    """
    engine_assumes, _, worst_ever = measure(profile)

    assert engine_assumes > worst_ever


@pytest.mark.parametrize("profile", list(PROFILES))
def test_the_over_reservation_is_large_enough_to_change_the_decision(profile: str) -> None:
    """Against a p99 household, the engine holds back hundreds of dollars it did not need to.

    That is not a rounding error — it is comparable to the entire default $750 buffer, so on
    many days it is the whole difference between sweeping and refusing.
    """
    engine_assumes, real_p99, _ = measure(profile)

    over_reserved = engine_assumes - real_p99

    assert over_reserved > 400.0


def test_variance_should_grow_with_the_square_root_of_the_horizon_not_linearly() -> None:
    """The mechanism behind the finding, isolated.

    A 30-day sum of iid daily spend has 30x the mean but only sqrt(30) ~ 5.5x the standard
    deviation. The current model effectively applies the daily quantile's *whole* deviation on
    all 30 days — inflating the padding by roughly sqrt(30).
    """
    daily = [float(x) for x in household(*PROFILES["steady"]).discretionary_series()]

    windows = [sum(daily[i : i + HORIZON_DAYS]) for i in range(len(daily) - HORIZON_DAYS + 1)]

    daily_sd = (sum((x - sum(daily) / len(daily)) ** 2 for x in daily) / len(daily)) ** 0.5
    window_sd = (sum((w - sum(windows) / len(windows)) ** 2 for w in windows) / len(windows)) ** 0.5

    # If variance grew linearly (the engine's implicit assumption), this ratio would be ~30.
    # It is ~sqrt(30) = 5.5. Bounds are loose because real spend is skewed, not Gaussian.
    ratio = window_sd / daily_sd

    assert 3.0 < ratio < 9.0
    assert ratio < HORIZON_DAYS / 2  # nowhere near linear

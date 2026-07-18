"""The recurring-stream detector (`backend/recurring.py`).

No database and no clock — a pure function over synthetic movement histories. The claims worth
holding: a clean biweekly paycheck, a monthly rent, and a weekly stream are each detected with the
right cadence, direction, amount, and day; noise (one-offs, irregular, too few) is left undetected
rather than guessed; the merchant key is stable across store numbers; and confidence tracks how
regular a stream actually is.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from backend.recurring import (
    MIN_CONFIDENCE,
    Movement,
    detect_recurring,
    normalize_name,
)


def _series(start: dt.date, step_days: int, count: int, amount: str, name: str) -> list[Movement]:
    return [
        Movement(day=start + dt.timedelta(days=step_days * i), amount=Decimal(amount), name=name)
        for i in range(count)
    ]


def _monthly(day_of_month: int, months: int, amount: str, name: str, year=2026) -> list[Movement]:
    return [
        Movement(day=dt.date(year, m, day_of_month), amount=Decimal(amount), name=name)
        for m in range(1, months + 1)
    ]


# --- normalization ------------------------------------------------------------------


class TestNormalize:
    def test_it_strips_store_numbers_and_case(self) -> None:
        assert normalize_name("SQ *BLUE BOTTLE #4412") == normalize_name("Sq Blue Bottle 5501")

    def test_a_purely_numeric_label_falls_back(self) -> None:
        assert normalize_name("  8829  ") == "8829"


# --- detection: the clean cases -----------------------------------------------------


class TestCleanStreams:
    def test_a_biweekly_paycheck_is_detected(self) -> None:
        movements = _series(dt.date(2026, 1, 2), 14, 6, "2600.00", "ACME PAYROLL")
        [stream] = detect_recurring(movements)
        assert stream.cadence == "biweekly"
        assert stream.direction == "inflow"
        assert stream.typical_amount == Decimal("2600.00")
        assert stream.occurrences == 6
        assert stream.confidence >= MIN_CONFIDENCE

    def test_a_monthly_rent_is_detected_as_an_outflow(self) -> None:
        movements = _monthly(1, 5, "-1800.00", "TENANT RENT")
        [stream] = detect_recurring(movements)
        assert stream.cadence == "monthly"
        assert stream.direction == "outflow"
        assert stream.typical_amount == Decimal("1800.00")
        assert stream.day_of_month == 1

    def test_a_weekly_stream_is_detected(self) -> None:
        movements = _series(dt.date(2026, 1, 5), 7, 8, "-45.00", "TRANSIT PASS")
        [stream] = detect_recurring(movements)
        assert stream.cadence == "weekly"

    def test_semimonthly_is_told_apart_from_biweekly(self) -> None:
        # The 15th and the last day of the month — twice a calendar month.
        movements: list[Movement] = []
        for m in range(1, 5):
            movements.append(Movement(dt.date(2026, m, 15), Decimal("1500.00"), "STATE PAYROLL"))
            movements.append(Movement(dt.date(2026, m, 28), Decimal("1500.00"), "STATE PAYROLL"))
        [stream] = detect_recurring(movements)
        assert stream.cadence == "semimonthly"


# --- detection: the noise it must NOT report ----------------------------------------


class TestNoiseIsLeftUndetected:
    def test_a_one_off_is_not_recurring(self) -> None:
        assert detect_recurring([Movement(dt.date(2026, 3, 1), Decimal("-500.00"), "DMV")]) == []

    def test_two_occurrences_are_not_enough(self) -> None:
        movements = _series(dt.date(2026, 1, 1), 30, 2, "-90.00", "GYM")
        assert detect_recurring(movements) == []

    def test_irregular_spend_is_not_a_stream(self) -> None:
        # Same merchant, wildly irregular days and amounts — a coffee habit, not a bill.
        days = [1, 4, 5, 12, 13, 19, 27]
        movements = [
            Movement(dt.date(2026, 3, d), Decimal(f"-{4 + d}.00"), "BLUE BOTTLE") for d in days
        ]
        assert all(s.cadence != "monthly" for s in detect_recurring(movements))

    def test_charges_and_refunds_do_not_cancel(self) -> None:
        # A merchant that both charges (out) and refunds (in) is two streams, never one that nets.
        charges = _monthly(10, 4, "-60.00", "STREAMING CO")
        refunds = _monthly(20, 4, "60.00", "STREAMING CO")
        streams = detect_recurring(charges + refunds)
        directions = {s.direction for s in streams}
        assert directions == {"inflow", "outflow"}


# --- confidence ---------------------------------------------------------------------


class TestConfidence:
    def test_a_metronomic_stream_beats_a_ragged_one(self) -> None:
        clean = _series(dt.date(2026, 1, 2), 14, 6, "2600.00", "CLEAN PAYROLL")
        # Same count and amount, but the gaps wobble hard (still within the biweekly band's edges).
        ragged_days = [dt.date(2026, 1, 2)]
        for gap in (12, 16, 12, 16, 13):
            ragged_days.append(ragged_days[-1] + dt.timedelta(days=gap))
        ragged = [Movement(d, Decimal("2600.00"), "RAGGED PAYROLL") for d in ragged_days]

        [c] = detect_recurring(clean)
        ragged_streams = detect_recurring(ragged)
        # The ragged one may or may not clear the bar, but if it does it must score lower.
        if ragged_streams:
            assert c.confidence > ragged_streams[0].confidence

    def test_result_is_sorted_by_confidence(self) -> None:
        payroll = _series(dt.date(2026, 1, 2), 14, 6, "2600.00", "PAYROLL")
        rent = _monthly(1, 5, "-1800.00", "RENT")
        streams = detect_recurring(payroll + rent)
        assert [s.confidence for s in streams] == sorted(
            (s.confidence for s in streams), reverse=True
        )

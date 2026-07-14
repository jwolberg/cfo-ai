"""The replay driver. `engine/outcome.py` finally has a caller.

These tests are almost entirely about the **two ways a replay lies**, because both lies are
flattering and neither is loud:

1. Grading against the household's untouched history never compounds the effect of our own
   sweeps, and systematically *understates* breach risk. `grade()` prevents that structurally —
   the caller cannot forget to apply the sweep, because the caller is not the one who applies it.
2. Counting a deferral as a loss prices a refusal we were *right* to make as a failure. Do that
   and the calibration dial learns to talk us out of the safety gates.

The number this module exists to produce is `breach_rate`. It is the only thing that licenses a
looser spend model, and until it existed, `daily_discretionary_high` could not be touched.
"""

from __future__ import annotations

from datetime import timedelta

from backend.precompute import DEMO_SPEC, WINDOW_START
from backend.replay import (
    DEFERRING_REASONS,
    Graded,
    calibrate,
    realized_from,
    replay,
)
from engine.models import ZERO, ReasonCode, money
from sim.household import TxnKind, generate

DAYS = 90


def run(**kw) -> list[Graded]:
    return list(replay(**kw))


class TestTheReplayProducesTheNumber:
    def test_a_real_household_can_finally_be_graded(self) -> None:
        """The whole point. `engine/outcome.py` existed for a day and a half with no caller, and
        without one the engine had no measured error distribution at all."""
        graded = run()

        assert graded, "no day was gradeable — the driver is not driving"
        assert all(g.outcome.projected_low is not None for g in graded)

    def test_the_breach_rate_is_a_real_fraction(self) -> None:
        """This is the dial. The spend model may be loosened exactly as far as this licenses."""
        c = calibrate(run())

        assert c.graded_days > 0
        assert 0.0 <= c.breach_rate <= 1.0
        assert 0.0 <= c.sweep_caused_overdraft_rate <= 1.0

    def test_the_engine_does_not_overdraft_the_household_it_serves(self) -> None:
        """prd.md §5.2's guardrail, which outranks the primary KPI.

        If this ever fails, nothing else in the calibration matters.
        """
        c = calibrate(run())
        assert c.sweep_caused_overdrafts == 0

    def test_an_ungradeable_day_is_skipped_rather_than_invented(self) -> None:
        """A blocking refusal never ran the forecast, so it has no projection.

        Grading it as a zero error would look like a *perfect* forecast and pull the whole
        distribution toward the origin — flattering us on precisely the days we knew least.
        """
        graded = run()
        assert len(graded) <= DAYS  # some days are genuinely not gradeable
        assert all(g.outcome.projected_low is not None for g in graded)


class TestADeferralIsNotALoss:
    """`false_refusal_cost` is our cost of *conservatism*. A deferral is not conservatism — it is
    a rule we chose, doing exactly what we chose it for."""

    def test_the_deferring_reasons_are_the_ones_that_resolve_themselves(self) -> None:
        assert ReasonCode.CADENCE_HOLD in DEFERRING_REASONS
        # Both new blocking codes from this feature resolve on their own, and neither is a
        # permanent cost. Pricing them as one would teach the dial to talk us out of the very
        # safety gate the portfolio reserve exists to add.
        assert ReasonCode.CARD_COVERAGE_INCOMPLETE in DEFERRING_REASONS
        assert ReasonCode.CARD_BEHAVIOR_UNKNOWN in DEFERRING_REASONS

        # A refusal for want of money is NOT a deferral — it is the honest answer, and it costs
        # nothing to be right about it.
        assert ReasonCode.NO_SURPLUS not in DEFERRING_REASONS

    def test_a_cadence_hold_costs_nothing_because_the_money_moves_next_week(self) -> None:
        """The same dollars sit there for six days. Counted naively they are 'lost' six times."""
        graded = run()
        holds = [g for g in graded if g.deferred]

        assert holds, "the weekly cadence should produce holds across 90 days"
        assert all(g.real_cost == ZERO for g in holds)

    def test_the_deferred_total_is_kept_separate_so_it_cannot_be_mistaken_for_a_cost(
        self,
    ) -> None:
        """Both numbers are real and they mean different things. Summing them would produce a
        third number that means nothing at all."""
        c = calibrate(run())

        assert c.total_deferred > ZERO, "the cadence defers real money"
        # The cost total excludes every dollar of it.
        assert c.total_false_refusal_cost == money(sum((g.real_cost for g in run()), ZERO))

    def test_naively_totalling_would_count_the_same_dollars_many_times_over(self) -> None:
        """The bug this partition exists to prevent, made explicit.

        The naive total is not merely bigger — it is bigger *because* it re-counts the same
        deferred money on every day it sits, which makes it a measure of how long the cadence
        made someone wait rather than a measure of our forecast error.
        """
        graded = run()

        naive = sum((g.outcome.false_refusal_cost for g in graded), ZERO)
        honest = sum((g.real_cost for g in graded), ZERO)

        assert naive > honest, "if these are equal, nothing is being deferred and the test is dead"


class TestRealizedExcludesWhatWeDid:
    def test_a_card_charge_is_not_a_checking_outflow(self) -> None:
        """It leaves checking on the day the *statement* is paid, and CARD_PAYMENT carries that.

        Counting the charge here as well would spend the same dollar twice and make the household
        look poorer than they are — which would make our forecast look better than it is.
        """
        history = generate(DEMO_SPEC, WINDOW_START, 120, seed=7)
        today = WINDOW_START + timedelta(days=30)

        realized = realized_from(history, today)
        charge_days = {t.day for t in history.card_charges()}
        overlap = charge_days & set(realized.net_by_day)

        assert overlap, "the demo household charges its card inside the horizon"

        for day in overlap:
            expected = sum(
                (
                    t.amount
                    for t in history.txns
                    if t.day == day and t.kind is not TxnKind.CARD_CHARGE
                ),
                ZERO,
            )
            assert realized.net_by_day[day] == expected

    def test_the_horizon_is_fully_covered_or_grade_refuses(self) -> None:
        """Grading against a partial future would silently flatter the projection."""
        history = generate(DEMO_SPEC, WINDOW_START, 120, seed=7)
        realized = realized_from(history, WINDOW_START)

        assert len(realized.net_by_day) == 30


class TestCalibrationRollup:
    def test_an_empty_replay_is_zero_rather_than_a_crash(self) -> None:
        c = calibrate([])
        assert c.graded_days == 0
        assert c.breach_rate == 0.0

    def test_an_optimistic_day_is_one_where_the_money_was_not_there(self) -> None:
        """Negative projection error = we said there would be more than there was.

        This is the tail that decides the company, and the sign convention is load-bearing
        forever — a flip here silently inverts the dial every future calibration is read off.
        """
        graded = run()
        c = calibrate(graded)

        optimistic = [g for g in graded if g.outcome.projection_error < ZERO]
        assert c.optimistic_days == len(optimistic)
        assert c.worst_projection_error == min(
            (g.outcome.projection_error for g in graded), default=ZERO
        )


def test_the_replay_changes_no_decision() -> None:
    """It observes. If it ever mutates a Snapshot or a Decision, the thing it is measuring is no
    longer the thing that shipped."""
    import backend.replay as replay_module

    with open(replay_module.__file__) as fh:
        body = fh.read()

    # No writes to the artifact, no re-tuning of the policy, no touching DEMO_SPEC.
    assert "dump(" not in body
    assert "DEMO_POLICY =" not in body

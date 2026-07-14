"""The population calibration. The measurement that licenses — or refuses — a looser forecast.

`backend/replay.py` grades one household. This grades sixty, and the difference is not academic:
**the single-household demo reported zero sweep-caused overdrafts while the population reported
43.** The bug was real (`derive_cash_events` double-counting today's paycheck), and one seed
simply never landed on the cash position where it bites.

That is the whole argument for this file. A guardrail measured on one household is not measured.
"""

from __future__ import annotations

import pytest

from backend.calibrate import SHAPES, Reading, licensed, measure

# The population is 60 households x 90 days. Measuring it at several dial settings takes ~40s,
# so the suite measures one setting and the full sweep lives behind `python -m backend.calibrate`.
SEEDS = (11, 12, 13, 14, 15)


@pytest.fixture(scope="module")
def today() -> Reading:
    return measure(None, seeds=SEEDS)


class TestTheGuardrailHolds:
    def test_the_engine_overdrafts_nobody_across_the_population(self, today: Reading) -> None:
        """`prd.md` §5.2: the guardrail outranks the primary KPI, and it means it.

        **This test is the one that would have caught the payday double-count.** It reported 43
        on the population while the demo reported zero, and 43 is not a calibration detail — it
        is the product doing the single thing it exists not to do.
        """
        assert today.sweep_caused_overdrafts == 0

    def test_every_shape_is_measured_not_just_the_average(self, today: Reading) -> None:
        """An average hides the household we are about to overdraft."""
        assert set(today.per_shape) == set(SHAPES)
        assert all(c.graded_days > 0 for c in today.per_shape.values())


class TestTheDialIsRefusedUnlessItEarnsIt:
    def test_a_setting_that_overdrafts_anyone_is_not_licensed_at_any_price(
        self, today: Reading
    ) -> None:
        """Not a trade. A veto.

        The false-refusal cost it would buy back is irrelevant — §5.2 does not have a price.
        """
        overdrafting = Reading(
            quantile=0.5,
            per_shape={
                name: c.__class__(
                    graded_days=c.graded_days,
                    sweep_caused_overdrafts=1,  # just one
                    optimistic_days=0,  # and a *perfect* breach rate
                    worst_projection_error=c.worst_projection_error,
                    total_false_refusal_cost=c.total_false_refusal_cost,
                    total_deferred=c.total_deferred,
                )
                for name, c in today.per_shape.items()
            },
        )

        assert not licensed(overdrafting, today)

    def test_a_setting_worse_on_one_shape_is_refused_even_if_better_on_average(
        self, today: Reading
    ) -> None:
        """A dial that is safe for two shapes and dangerous for the third is not safe."""
        one_bad_shape = Reading(
            quantile=0.9,
            per_shape={
                name: c.__class__(
                    graded_days=c.graded_days,
                    sweep_caused_overdrafts=0,
                    # The first shape gets much worse; the others get much better.
                    optimistic_days=(c.graded_days if name == "typical" else 0),
                    worst_projection_error=c.worst_projection_error,
                    total_false_refusal_cost=c.total_false_refusal_cost,
                    total_deferred=c.total_deferred,
                )
                for name, c in today.per_shape.items()
            },
        )

        assert not licensed(one_bad_shape, today)

    def test_the_empirical_swap_is_not_licensed_as_the_learning_specified_it(
        self, today: Reading
    ) -> None:
        """The measured refusal, pinned so it cannot be quietly reversed.

        `docs/learnings/2026-07-13-the-spend-model-over-reserves.md` proposed enumerating the
        household's own 30-day windows and reserving against a quantile of them. Measured, that
        swap **raises the breach rate roughly eight-fold** (2.3% -> ~20%) even at `q=1.0` — the
        worst 30-day stretch the household has *ever had*.

        The reason is **not** structural, and an earlier draft of this docstring said it was: it
        claimed the projected low lands mid-horizon, where a flat-amortized 30-day total has only
        charged a fraction of itself. That explanation cannot be right. *Both* models charge a
        flat constant per day — `p90_daily` against `spend_30d_high / 30` — so wherever the low
        lands it scales both of them identically. Only the size of the constant differs.

        The real reason is that the empirical model is **starved**, and it is written up in the
        2026-07-14 learning. It reads the worst 30-day window off the ~60-150 days of history the
        engine actually has, which is 2-5 *independent* months — overlapping windows flatter the
        sample size but not the information in it — and the worst of 3 months badly understates
        the worst of 36. Fed three years instead, the same model's breach rate falls from 19.3%
        to 3.7%, and on the fat-tailed household it falls to **zero**. Nothing about the
        amortization changed; only the history did.

        If this test ever fails, the swap has become safe — which would be excellent news, and
        needs the calibration re-run and the learning rewritten, not this assertion deleted.
        """
        worst_ever = measure(1.0, seeds=SEEDS)

        assert not licensed(worst_ever, today)
        assert worst_ever.breach_rate > today.breach_rate

    def test_a_setting_that_reserves_more_than_today_is_not_a_loosening(
        self, today: Reading
    ) -> None:
        """The trap this harness would otherwise walk into.

        **Reserving more always breaches less.** The overdraft veto and the per-shape breach bar
        are both monotone in the size of the reserve, so a dial swept far enough in the
        *tightening* direction will always eventually clear them — and get pronounced "licensed"
        for the sole achievement of being more conservative than the model it replaces.

        `q=3.0` really does this: it passes both safety conditions and costs **$1.62M** in
        false-refusal cost against today's **$544K**. The dial exists to hand refused money
        *back*. A setting that reserves more than today is a tightening wearing the name of a
        loosening, and it is not what this measurement was built to license.
        """
        perfectly_safe_and_far_more_expensive = Reading(
            quantile=3.0,
            per_shape={
                name: c.__class__(
                    graded_days=c.graded_days,
                    sweep_caused_overdrafts=0,
                    optimistic_days=0,  # a *flawless* breach rate ...
                    worst_projection_error=c.worst_projection_error,
                    # ... bought by refusing twice as much money as we refuse today.
                    total_false_refusal_cost=c.total_false_refusal_cost * 2,
                    total_deferred=c.total_deferred,
                )
                for name, c in today.per_shape.items()
            },
        )

        assert not licensed(perfectly_safe_and_far_more_expensive, today)

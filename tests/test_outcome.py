"""Grading a decision against what actually happened. These tests are the spec.

`decide()` emits a prediction; until this module existed, nothing in the repo ever settled the
bet. Every field here is a metric the PRD or the strategy names by hand:

- `sweep_caused_overdraft` — prd.md §5.2's guardrail, the hard gate that outranks the KPI.
- `projection_error`      — strategy.md §3's "empirical distribution of our own errors", the
                            only asset that compounds.
- `false_refusal_cost`    — prd.md §5.3's "cost of conservatism", and the only instrument that
                            would ever reveal the spend-model bug in decision-engine.md [6.5].

The load-bearing test in this file is
`test_cannot_grade_a_sweep_against_a_ledger_that_does_not_reflect_it`.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from engine.decide import decide
from engine.forecast import HORIZON_DAYS
from engine.models import (
    Account,
    Action,
    ConnectionState,
    Debt,
    Decision,
    Reason,
    ReasonCode,
    Snapshot,
    UserPolicy,
    money,
)
from engine.outcome import Realized, grade

TODAY = date(2026, 7, 13)


def account(balance: str, **kw) -> Account:
    base = dict(
        account_id="chk",
        balance=money(balance),
        connection=ConnectionState.HEALTHY,
        balance_age_days=0,
    )
    base.update(kw)
    return Account(**base)


def card(balance: str = "9000.00", **kw) -> Debt:
    base = dict(
        debt_id="visa",
        balance=money(balance),
        minimum_payment=money("180.00"),
        minimum_due_date=TODAY + timedelta(days=20),
        apr=Decimal("0.2399"),
        observed_monthly_payment=money("400.00"),
    )
    base.update(kw)
    return Debt(**base)


def snapshot(**kw) -> Snapshot:
    base = dict(
        today=TODAY,
        accounts=(account("4000.00"),),
        funding_account_id="chk",
        events=(),
        pending=(),
        debts=(card(),),
        policy=UserPolicy(
            buffer_floor=money("750.00"),
            max_sweep=money("300.00"),
            max_weekly_sweep=money("600.00"),
        ),
        daily_discretionary_high=money("40.00"),
        income_variation=0.05,
        history_days=180,
    )
    base.update(kw)
    return Snapshot(**base)


def flat(daily_net: str) -> Realized:
    """The household nets the same amount on each of the 30 days after today.

    Today itself is not in here — today's movements are already inside the snapshot balance.
    """
    return Realized(
        net_by_day={TODAY + timedelta(days=i): money(daily_net) for i in range(1, HORIZON_DAYS + 1)}
    )


def swept(amount: str, projected_low: str) -> Decision:
    return Decision(
        action=Action.SWEEP,
        amount=money(amount),
        target_debt_id="visa",
        reasons=(Reason(ReasonCode.PROJECTION, {}),),
        projected_low_balance=money(projected_low),
    )


def refused(projected_low: str) -> Decision:
    return Decision(
        action=Action.REFUSE,
        amount=money("0.00"),
        target_debt_id=None,
        reasons=(Reason(ReasonCode.NO_SURPLUS, {}),),
        projected_low_balance=money(projected_low),
    )


class TestTheSweepIsApplied:
    """The bug that would make every shadow-mode report a lie.

    A replay that grades each day's decision against the household's *untouched* history never
    compounds the effect of its own sweeps, and will systematically understate breach risk. So
    `Realized` carries the household's own movements and **the grader applies our sweep
    itself** — the caller is never trusted to have remembered.
    """

    def test_cannot_grade_a_sweep_against_a_ledger_that_does_not_reflect_it(self) -> None:
        """The swept dollar must be gone from the realized path. Structurally, not by
        convention.

        $4,000 opening, household spends $100/day for 30 days -> unswept low is $1,000.
        We swept $300, so the realized low must be $700 — not $1,000.
        """
        outcome = grade(snapshot(), swept("300.00", projected_low="900.00"), flat("-100.00"))

        assert outcome.realized_low_unswept == money("1000.00")
        assert outcome.realized_low == money("700.00")

    def test_a_refusal_moves_nothing_so_the_two_paths_agree(self) -> None:
        outcome = grade(snapshot(), refused(projected_low="900.00"), flat("-100.00"))

        assert outcome.realized_low == outcome.realized_low_unswept == money("1000.00")


class TestProjectionError:
    """The calibration asset. Signed, and the sign is load-bearing forever."""

    def test_a_conservative_engine_has_a_positive_error(self) -> None:
        """Realized better than projected. We under-swept; nobody was hurt."""
        outcome = grade(snapshot(), refused(projected_low="600.00"), flat("-100.00"))

        assert outcome.projection_error == money("400.00")  # 1000 realized - 600 projected

    def test_an_optimistic_engine_has_a_negative_error(self) -> None:
        """Realized WORSE than projected. This is the tail that decides the company."""
        outcome = grade(snapshot(), refused(projected_low="1500.00"), flat("-100.00"))

        assert outcome.projection_error == money("-500.00")

    def test_the_error_is_measured_against_the_households_own_path_not_ours(self) -> None:
        """The distinction that keeps the calibration asset clean.

        `projected_low_balance` is a projection of the household's **pre-sweep** trajectory —
        `decide()` computes the sweep *from* it. So the forecast is graded against the unswept
        path. Grade it against the post-sweep low instead and the error absorbs the size of our
        own sweep, making the engine look wildly optimistic on exactly the days it swept hardest
        and was perfectly right.

        Here the forecast was exact: it said the household's low would be $1,000, and it was.
        The error is zero — even though we then swept $300 and left them at $700.
        """
        outcome = grade(snapshot(), swept("300.00", projected_low="1000.00"), flat("-100.00"))

        assert outcome.realized_low_unswept == money("1000.00")
        assert outcome.realized_low == money("700.00")  # what our sweep left them with
        assert outcome.projection_error == money("0.00")  # but the FORECAST was perfect

    def test_the_size_of_our_own_sweep_never_moves_the_projection_error(self) -> None:
        """The property, stated directly. A bigger sweep is not a worse forecast."""
        small = grade(snapshot(), swept("50.00", projected_low="900.00"), flat("-100.00"))
        large = grade(snapshot(), swept("300.00", projected_low="900.00"), flat("-100.00"))

        assert small.projection_error == large.projection_error == money("100.00")
        assert small.realized_low != large.realized_low  # but the outcomes do differ


class TestOverdraft:
    """prd.md §5.2 — the guardrail that outranks the primary KPI."""

    def test_a_sweep_that_pushes_them_under_zero_is_a_sweep_caused_overdraft(self) -> None:
        """$200 opening, spends $10/day -> unswept low is -$100... they were going under
        anyway.

        So use a household that stays *just* solvent on its own and is pushed under by us.
        """
        s = snapshot(accounts=(account("3100.00"),))
        # spends 100/day for 30 days -> unswept low = 100. Solvent, barely.
        outcome = grade(s, swept("300.00", projected_low="900.00"), flat("-100.00"))

        assert outcome.realized_low_unswept == money("100.00")
        assert outcome.realized_low == money("-200.00")
        assert outcome.overdrafted
        assert outcome.sweep_caused_overdraft

    def test_a_household_that_overdrafts_on_its_own_is_not_our_fault(self) -> None:
        """The distinction §5.2 draws by saying **sweep-caused**.

        They were going below zero with or without us. We are liable for the difference we
        made, not for their life. Conflating the two would make the guardrail metric
        meaningless — it would be dominated by households we never touched.
        """
        s = snapshot(accounts=(account("2000.00"),))
        outcome = grade(s, refused(projected_low="500.00"), flat("-100.00"))

        assert outcome.realized_low_unswept == money("-1000.00")
        assert outcome.overdrafted
        assert not outcome.sweep_caused_overdraft

    def test_a_sweep_into_a_household_already_going_under_is_still_not_sweep_caused(
        self,
    ) -> None:
        """We made it worse, but we did not cause it. Reported honestly as both."""
        s = snapshot(accounts=(account("2000.00"),))
        outcome = grade(s, swept("300.00", projected_low="500.00"), flat("-100.00"))

        assert outcome.overdrafted
        assert not outcome.sweep_caused_overdraft
        assert outcome.realized_low == money("-1300.00")

    def test_breaching_the_buffer_is_not_the_same_as_an_overdraft(self) -> None:
        """The buffer did its job — it absorbed the miss. Worth knowing; not a failure."""
        s = snapshot(accounts=(account("3500.00"),))
        outcome = grade(s, swept("300.00", projected_low="900.00"), flat("-100.00"))

        assert outcome.realized_low == money("200.00")  # 3500 - 3000 - 300
        assert outcome.breached_buffer  # below the $750 floor
        assert not outcome.overdrafted


class TestFalseRefusalCost:
    """prd.md §5.3 — "money we left idle that was genuinely safe to move"."""

    def test_a_refusal_that_was_wrong_costs_us_the_whole_safe_amount(self) -> None:
        """Unswept low $1,000, buffer $750, reserved $180 -> $70 was genuinely safe.

        $70 is under the $300 per-sweep cap, so the guardrails were not what stopped us. Our
        forecast was. All $70 is our cost.
        """
        outcome = grade(snapshot(), refused(projected_low="600.00"), flat("-100.00"))

        assert outcome.hindsight_safe == money("70.00")
        assert outcome.should_have_swept == money("70.00")
        assert outcome.false_refusal_cost == money("70.00")

    def test_the_users_own_cap_is_not_our_conservatism(self) -> None:
        """The correction that this module's first draft got wrong.

        Hindsight says $6,070 was sitting there safely. But the user capped us at $300 a sweep
        and we moved $300 — we were not being conservative, we were being **obedient**. Blaming
        ourselves for their guardrail would flood §5.3's metric with noise and drown the signal
        it exists to carry.
        """
        s = snapshot(accounts=(account("10000.00"),))
        # unswept low = 10000 - 3000 = 7000. Raw surplus = 7000 - 750 - 180 = 6070.
        outcome = grade(s, swept("300.00", projected_low="900.00"), flat("-100.00"))

        assert outcome.hindsight_safe == money("6070.00")  # what was really there
        assert outcome.should_have_swept == money("300.00")  # what we were allowed to take
        assert outcome.false_refusal_cost == money("0.00")  # so we cost the user nothing

    def test_under_sweeping_costs_us_too_not_just_refusing(self) -> None:
        """The signature of the spend-model bug ([6.5]) — it does not refuse, it under-sweeps.

        A metric that only fired on outright refusals would miss it entirely. Here the cap is
        raised out of the way so the forecast error is the only thing left to blame.
        """
        s = snapshot(
            accounts=(account("6000.00"),),
            policy=UserPolicy(
                buffer_floor=money("750.00"),
                max_sweep=money("5000.00"),
                max_weekly_sweep=money("5000.00"),
            ),
        )
        # unswept low = 6000 - 3000 = 3000. safe = 3000 - 750 - 180 = 2070, under both caps.
        outcome = grade(s, swept("300.00", projected_low="900.00"), flat("-100.00"))

        assert outcome.should_have_swept == money("2070.00")
        assert outcome.false_refusal_cost == money("1770.00")  # 2070 - 300 actually moved

    def test_a_correct_refusal_costs_nothing(self) -> None:
        """There was genuinely nothing safe to move. The refusal was the product working."""
        s = snapshot(accounts=(account("3800.00"),))
        # unswept low = 800. safe = 800 - 750 - 180 = negative -> zero.
        outcome = grade(s, refused(projected_low="700.00"), flat("-100.00"))

        assert outcome.hindsight_safe == money("0.00")
        assert outcome.false_refusal_cost == money("0.00")

    def test_sweeping_more_than_was_safe_is_not_a_negative_cost(self) -> None:
        """Over-sweeping is a breach, not a bonus. It must never net off a false refusal."""
        s = snapshot(accounts=(account("3800.00"),))
        outcome = grade(s, swept("300.00", projected_low="900.00"), flat("-100.00"))

        assert outcome.false_refusal_cost == money("0.00")
        assert outcome.breached_buffer


class TestTheInterestClaim:
    def test_the_grade_records_what_we_claimed(self) -> None:
        real = decide(snapshot())
        outcome = grade(snapshot(), real, flat("-100.00"))

        assert real.action is Action.SWEEP
        assert outcome.interest_claimed is not None
        assert outcome.interest_claimed > money("0.00")

    def test_no_claim_recorded_when_the_engine_made_none(self) -> None:
        outcome = grade(snapshot(), swept("300.00", projected_low="900.00"), flat("-100.00"))

        assert outcome.interest_claimed is None


class TestRefusalsAreGradedToo:
    def test_a_refusal_produces_a_full_outcome(self) -> None:
        """An ungraded refusal is how a system under-sweeps quietly forever ([3.1])."""
        outcome = grade(snapshot(), refused(projected_low="600.00"), flat("-100.00"))

        assert outcome.action is Action.REFUSE
        assert outcome.swept == money("0.00")
        assert outcome.projection_error is not None
        assert outcome.false_refusal_cost == money("70.00")


class TestAgainstRealGeneratedHouseholds:
    """The round trip the whole harness rests on: `sim/` in, a graded decision out.

    Everything above uses hand-built ledgers to pin the arithmetic. These use households whose
    realized future was generated rather than hand-chosen, which is the only way to find out
    whether the two halves actually fit.
    """

    def household(self, **shock_kw):
        from sim.household import (
            BillSpec,
            CardSpec,
            HouseholdSpec,
            PayCadence,
            PayrollSpec,
            ShockSpec,
            SpendSpec,
            generate,
        )

        spec = HouseholdSpec(
            opening_balance=money("4000.00"),
            payroll=PayrollSpec(
                net_pay=money("2600.00"),
                cadence=PayCadence.BIWEEKLY,
                first_payday=date(2026, 1, 2),
                variation=Decimal("0.02"),
            ),
            bills=(BillSpec(label="rent", day_of_month=1, mean=money("1800.00")),),
            spend=SpendSpec(zero_day_probability=0.25, median=money("38.00"), log_sigma=0.9),
            card=CardSpec(
                balance=money("9000.00"),
                apr=Decimal("0.2399"),
                minimum_payment=money("180.00"),
                payment=money("400.00"),
            ),
            shocks=ShockSpec(**shock_kw),
        )
        return generate(spec, start=date(2026, 1, 1), days=180, seed=7)

    def realized_from(self, history, today: date) -> Realized:
        """Turn a household's actual future into the answer key, one net figure per day."""
        return Realized(
            net_by_day={
                today + timedelta(days=i): history.balance_on(today + timedelta(days=i))
                - history.balance_on(today + timedelta(days=i - 1))
                for i in range(1, HORIZON_DAYS + 1)
            }
        )

    def snapshot_on(self, history, today: date, **kw) -> Snapshot:
        # The card's due date has to be relative to *this* day, or it falls outside the horizon
        # and nothing gets reserved — which would quietly make every integration number here
        # describe a household with no minimum payment due.
        return snapshot(
            today=today,
            accounts=(account(str(history.balance_on(today))),),
            debts=(card(minimum_due_date=today + timedelta(days=20)),),
            **kw,
        )

    def test_a_real_household_grades_end_to_end(self) -> None:
        history = self.household()
        today = date(2026, 3, 1)

        s = self.snapshot_on(history, today)
        outcome = grade(s, decide(s), self.realized_from(history, today))

        # The realized low is a fact about the household, not about us.
        assert outcome.realized_low_unswept == min(
            history.daily_balances(today + timedelta(days=1), today + timedelta(days=HORIZON_DAYS))
        )
        assert outcome.realized_low == outcome.realized_low_unswept - outcome.swept

    def test_the_engine_is_conservative_on_a_household_it_was_built_for(self) -> None:
        """A stable W2 household with no shocks: the projection should not be optimistic.

        A negative projection error here would mean the engine is optimistic on exactly the
        household prd.md §3 says we serve — which would be a five-alarm result.
        """
        history = self.household()
        today = date(2026, 3, 1)
        s = self.snapshot_on(history, today)

        outcome = grade(s, decide(s), self.realized_from(history, today))

        assert outcome.projection_error >= money("0.00")
        assert not outcome.overdrafted
        assert not outcome.sweep_caused_overdraft

    def test_the_per_sweep_cap_currently_masks_the_spend_model_bug(self) -> None:
        """A finding in its own right, and one #4 must not be allowed to miss.

        Under the default $300 per-sweep cap, [6.5]'s over-reservation costs the user **nothing
        measurable** — the cap binds long before the forecast error does. `false_refusal_cost`
        reads zero, and a shadow-mode report run at these caps would pronounce the engine
        healthy.

        The bug is **latent**, not absent. It starts costing real money precisely when the
        ceiling is raised — which is exactly what prd.md §8's *"Then: raise the ceiling as
        calibration proves out"* sets out to do. So the moment the company begins trusting its
        calibration is the moment this bug begins to bite.

        Implication for #4: the replay driver **must sweep the cap across a range**, or it will
        measure a forecast error of zero and report a false clean bill of health.
        """
        history = self.household()
        today = date(2026, 3, 1)
        s = self.snapshot_on(history, today)

        outcome = grade(s, decide(s), self.realized_from(history, today))

        assert outcome.swept == money("300.00")  # the cap, not the forecast
        assert outcome.should_have_swept == money("300.00")
        assert outcome.false_refusal_cost == money("0.00")  # invisible, at these caps

        # And yet thousands were genuinely sitting there, untouched.
        assert outcome.hindsight_safe > money("7000.00")

    def test_raising_the_cap_exposes_the_spend_model_bug_immediately(self) -> None:
        """The same household, the same day, the same realized future — cap lifted.

        Now [6.5] is priced: the engine leaves four figures of genuinely safe money idle,
        purely because it charges a p90 of daily spend on all 30 horizon days. This is the
        instrument doing the job it was built for, and it is why `false_refusal_cost` had to
        fire on under-sweeps rather than only on refusals.
        """
        history = self.household()
        today = date(2026, 3, 1)
        s = self.snapshot_on(
            history,
            today,
            policy=UserPolicy(
                buffer_floor=money("750.00"),
                max_sweep=money("9000.00"),
                max_weekly_sweep=money("9000.00"),
            ),
        )

        outcome = grade(s, decide(s), self.realized_from(history, today))

        assert outcome.false_refusal_cost > money("500.00")
        assert outcome.should_have_swept > outcome.swept

    def test_uncapped_the_false_refusal_cost_is_exactly_the_projection_error(self) -> None:
        """An identity, and a cross-check on both metrics at once.

        With no cap binding, `decide()` sweeps `projected_low - buffer - reserved`, and perfect
        hindsight would have swept `realized_low - buffer - reserved`. The gap between them is
        `realized_low - projected_low` — which *is* the projection error.

        So `false_refusal_cost` is the forecast error priced in dollars, exactly. If these two
        ever drift apart under an uncapped policy, one of them has a bug, and this is the test
        that says so.
        """
        history = self.household()
        today = date(2026, 3, 1)
        s = self.snapshot_on(
            history,
            today,
            policy=UserPolicy(
                buffer_floor=money("750.00"),
                max_sweep=money("9000.00"),
                max_weekly_sweep=money("9000.00"),
            ),
        )

        outcome = grade(s, decide(s), self.realized_from(history, today))

        assert outcome.false_refusal_cost == outcome.projection_error
        assert outcome.projection_error > money("0.00")  # and the engine was conservative

    def test_a_missed_paycheck_is_caught_as_a_worse_realized_low(self) -> None:
        """The engine cannot see it coming — that is the point. The grader can, afterwards.

        This is what shadow mode is *for*: the households where the forecast is wrong are the
        only ones that teach us anything.
        """
        today = date(2026, 3, 1)
        payday = date(2026, 3, 13)

        healthy = self.household()
        struck = self.household(missed_paycheck_on=payday)

        s = self.snapshot_on(healthy, today)
        decision = decide(s)  # identical decision — the engine has no idea

        good = grade(s, decision, self.realized_from(healthy, today))
        bad = grade(s, decision, self.realized_from(struck, today))

        assert bad.realized_low < good.realized_low
        assert bad.projection_error < good.projection_error


class TestBadInput:
    def test_a_decision_with_no_projection_cannot_be_graded(self) -> None:
        """A blocking refusal never computed a low balance. There is nothing to grade it
        against, and inventing one would corrupt the calibration distribution with zeros."""
        blocked = Decision(
            action=Action.REFUSE,
            amount=money("0.00"),
            target_debt_id=None,
            reasons=(Reason(ReasonCode.CONNECTION_UNHEALTHY, {}),),
            projected_low_balance=None,
        )

        with pytest.raises(ValueError, match="no projection"):
            grade(snapshot(), blocked, flat("-100.00"))

    def test_a_realized_ledger_that_does_not_cover_the_horizon_is_refused(self) -> None:
        """Grading against a partial future would silently flatter the projection."""
        short = Realized(net_by_day={TODAY + timedelta(days=i): money("-100.00") for i in range(5)})

        with pytest.raises(ValueError, match="horizon"):
            grade(snapshot(), refused(projected_low="600.00"), short)

"""The day-by-day walk, and the artifact it writes.

The debt ledger and the rolling statistics are new stateful derivations with no precedent
in `engine/` or `sim/` to lean on, and every way they can be wrong is quiet: a balance that
drifts, a weekly cap that never binds, a percentile computed over the wrong window. So they
are pinned here directly, not just observed through a finished artifact.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from backend import artifact as art
from backend.precompute import (
    CHECKING_ID,
    DEMO_POLICY,
    DEMO_SPEC,
    ESTIMATED_APR,
    SAVINGS_BALANCE,
    SEED,
    SERVED_DAYS,
    STATEMENT_DAY,
    WARMUP_DAYS,
    WINDOW_START,
    DebtLedger,
    assemble_snapshot,
    build,
    classify_behavior,
    daily_discretionary_high,
    derive_card,
    derive_cash_events,
    derive_portfolio,
    derive_spend_profile,
    detect_unmatched_payments,
    income_variation,
    infer_close_day,
    observed_monthly_payment,
    spend_30d_high,
    walk,
)
from engine.forecast import HORIZON_DAYS
from engine.interest import total_interest
from engine.models import (
    ZERO,
    Action,
    AprSource,
    CoverageState,
    EventKind,
    PaymentBehavior,
    ReasonCode,
    UserPolicy,
    money,
)
from sim.household import (
    BillSpec,
    CardSpec,
    History,
    HouseholdSpec,
    PayCadence,
    PayrollSpec,
    SpendSpec,
    Txn,
    TxnKind,
    generate,
)


def spec_with(**kw) -> HouseholdSpec:
    """DEMO_SPEC with one or two fields swapped — the base is already the persona."""
    card_kw = {k: kw.pop(k) for k in list(kw) if k in CardSpec.__dataclass_fields__}
    card = CardSpec(**{**vars(DEMO_SPEC.card), **card_kw}) if card_kw else DEMO_SPEC.card
    spend = kw.pop("spend", DEMO_SPEC.spend)
    return HouseholdSpec(
        opening_balance=kw.pop("opening_balance", DEMO_SPEC.opening_balance),
        payroll=DEMO_SPEC.payroll,
        bills=DEMO_SPEC.bills,
        spend=spend,
        cards=(card,),
        shocks=DEMO_SPEC.shocks,
    )


class TestDebtLedger:
    def ledger(self, balance: str = "10000.00") -> DebtLedger:
        return DebtLedger(principal=money(balance), apr=Decimal("0.2399"))

    def test_a_quiet_day_still_grows_the_balance(self) -> None:
        """No payment, no sweep — the card still costs money. This is the whole product."""
        ledger = self.ledger()
        before = ledger.outstanding

        ledger.accrue(date(2026, 3, 5))  # not the statement day

        assert ledger.outstanding > before

    def test_interest_does_not_compound_within_the_cycle(self) -> None:
        """Unposted interest earns no interest — the average-daily-balance method.

        Two days of accrual on a flat principal must add exactly twice one day's interest.
        If accrued interest were folded into the accrual base, day two would be larger.
        """
        ledger = self.ledger()
        daily = ledger.principal * (Decimal("0.2399") / Decimal("365"))

        ledger.accrue(date(2026, 3, 5))
        ledger.accrue(date(2026, 3, 6))

        assert ledger.accrued == daily * 2

    def test_accrued_interest_posts_at_the_statement_close(self) -> None:
        ledger = self.ledger()
        ledger.accrue(date(2026, 3, 5))
        assert ledger.principal == money("10000.00")  # still unposted

        pending = ledger.accrued
        ledger.accrue(date(2026, 3, STATEMENT_DAY))

        assert ledger.accrued == Decimal(0)
        assert ledger.principal == money("10000.00") + money(
            pending + money("10000.00") * (Decimal("0.2399") / Decimal("365"))
        )

    def test_a_payment_lands_before_the_days_interest(self) -> None:
        """A dollar paid today does not accrue today — what makes an early sweep worth more."""
        early, late = self.ledger(), self.ledger()

        early.pay(money("1000.00"))
        early.accrue(date(2026, 3, 5))

        late.accrue(date(2026, 3, 5))
        late.pay(money("1000.00"))

        assert early.accrued < late.accrued

    def test_an_ordinary_payment_comes_off_the_principal(self) -> None:
        """Not off the unposted interest — the principal is the accrual base, and shrinking
        it is what a payment is *for*. This is also exactly what `engine/interest.py` does."""
        ledger = self.ledger()
        ledger.accrue(date(2026, 3, 5))
        accrued = ledger.accrued

        ledger.pay(money("100.00"))

        assert ledger.principal == money("10000.00") - money("100.00")
        assert ledger.accrued == accrued  # untouched

    def test_paying_the_full_outstanding_clears_the_card(self) -> None:
        """The engine sweeps `outstanding` when CLEARS_THE_CARD binds. It must actually clear.

        Retiring principal alone would strand the accrued interest and leave the card stuck
        on a balance of a few cents forever.
        """
        ledger = self.ledger("100.00")
        ledger.accrue(date(2026, 3, 5))

        ledger.pay(ledger.outstanding)

        assert ledger.outstanding == Decimal(0)

    def test_a_card_cannot_be_overpaid_into_credit(self) -> None:
        ledger = self.ledger("100.00")
        ledger.pay(money("500.00"))
        assert ledger.outstanding == Decimal(0)


class TestDerivedCashEvents:
    def test_bounds_are_a_range_not_a_point(self) -> None:
        """A certain forecast is not a conservative one.

        `forecast.py`'s entire safety property is the asymmetry it applies across these
        ranges. Collapse them to point estimates and the conservative low balance quietly
        becomes an exact one.
        """
        events = derive_cash_events(DEMO_SPEC, date(2026, 3, 1))
        payroll = [e for e in events if e.label == "payroll"]

        assert payroll
        for e in payroll:
            assert abs(e.amount_low) < abs(e.amount) < abs(e.amount_high)
            assert e.date_jitter_days > 0

    def test_inflows_and_outflows_are_signed_correctly(self) -> None:
        for e in derive_cash_events(DEMO_SPEC, date(2026, 3, 1)):
            assert e.is_inflow == (e.label == "payroll")

    def test_the_card_payment_is_tagged_and_the_reserve_carries_it(self) -> None:
        """The ORDINARY workaround is gone, and this test is its epitaph.

        It used to assert the opposite: the payment was emitted as ORDINARY at its full $450
        and `decide()` reserved only the $280 minimum on top, knowingly over-counting by $280
        because the alternative — tagging it so the forecast skipped it — under-counted $170,
        and only one of those directions ends in an overdraft.

        That workaround stood up only because the payment was a hardcoded constant. It is now
        a function of behaviour and the statement that closed, and `untouchable()` reserves the
        household's **actual obligation** rather than the minimum the issuer would settle for.
        So the payment is tagged, the forecast skips it, the reserve carries it, and the
        arithmetic is exact for the first time instead of deliberately wrong in the safe
        direction.

        The two halves are one mechanism: skip the event *without* reserving the obligation and
        nothing accounts for the payment at all.
        """
        events = derive_cash_events(DEMO_SPEC, date(2026, 3, 1))
        card = [e for e in events if e.label == "card payment"]

        assert card
        assert all(e.kind is EventKind.CARD_PAYMENT for e in card)
        assert all(e.amount == -DEMO_SPEC.card.payment for e in card)

    def test_events_land_on_the_funding_account(self) -> None:
        events = derive_cash_events(DEMO_SPEC, date(2026, 3, 1))
        assert all(e.account_id == CHECKING_ID for e in events)

    def test_only_the_next_thirty_days_are_forecast(self) -> None:
        today = date(2026, 3, 1)
        events = derive_cash_events(DEMO_SPEC, today)
        assert all(today <= e.expected_date <= today + timedelta(days=30) for e in events)


class TestRollingStats:
    def history(self, days: int = 150):
        return generate(DEMO_SPEC, start=date(2026, 1, 1), days=days, seed=7)

    def test_a_short_history_uses_only_what_it_has(self) -> None:
        """No lookahead, and no silently-shorter window either.

        The stat on day 10 must be computed from ten days — not from a 90-day window padded
        with zeros, and certainly not from days the household has not lived yet.
        """
        history = self.history()
        day = history.start + timedelta(days=9)

        seen = history.as_of(day)
        expected = daily_discretionary_high(seen, day)

        assert daily_discretionary_high(history, day) == expected

    def test_the_spend_percentile_sits_at_the_high_end(self) -> None:
        """p90 of a right-skewed distribution is well above its median."""
        history = self.history()
        day = history.start + timedelta(days=120)

        high = daily_discretionary_high(history, day)

        assert high > DEMO_SPEC.spend.median

    def test_a_biweekly_earner_reads_as_regular_income(self) -> None:
        """The gate is 25%. Calendar-month buckets would score this household at ~24%.

        Two paychecks a month, except the four times a year there are three — bucketing by
        month turns that calendar artifact into "volatile income" and refuses to serve a
        household whose pay is in fact identical every fortnight.
        """
        history = self.history()
        day = history.start + timedelta(days=120)

        assert income_variation(history, day) < 0.05

    def test_too_little_history_makes_no_claim_about_variability(self) -> None:
        history = self.history()
        day = history.start + timedelta(days=5)

        assert income_variation(history, day) == 0.0


class TestTheWalk:
    def test_the_served_window_shows_the_engine_doing_both(self) -> None:
        a = build()
        actions = {r.decision.action for r in a.days}
        assert actions == {Action.SWEEP, Action.REFUSE}

    def test_the_walk_is_blind_to_history_beyond_today(self) -> None:
        """`build()` and `replay()` drive the same `walk()` over histories of different lengths.

        `replay()` holds `HORIZON_DAYS + 1` extra days because grading needs the future the
        forecast was about. `build()` does not. One walk over both is only safe if those extra
        days are invisible to the decision — every derivation reads `history.as_of(today)`, which
        `History.as_of` calls "the seam that keeps lookahead out of the backtest." If one ever
        stopped, the artifact would ship one engine while the calibration graded another: ticket
        `0019`'s failure, wearing a different hat.

        Truncation, **not** a second `generate()`, is what makes this an honest test. `generate()`
        is not prefix-stable — same seed, different `days`, different household — so regenerating
        a shorter history would compare two unrelated households and prove nothing. The first
        draft of this test did exactly that and failed for that reason. See
        `TestGenerateIsNotPrefixStable`.
        """
        total = WARMUP_DAYS + SERVED_DAYS
        full = generate(DEMO_SPEC, WINDOW_START, total + HORIZON_DAYS + 1, seed=SEED)
        truncated = full.as_of(WINDOW_START + timedelta(days=total - 1))

        for a, b in zip(
            walk(truncated, DEMO_SPEC, WINDOW_START, total),
            walk(full, DEMO_SPEC, WINDOW_START, total),
            strict=True,
        ):
            assert a.snapshot == b.snapshot, f"{a.day}: the walk read past today"
            assert a.decision == b.decision, f"{a.day}: the decision moved on future data"

    def test_the_warm_up_days_are_not_served(self) -> None:
        """The feed opens on real decisions, not a wall of INSUFFICIENT_HISTORY refusals.

        The engine still refuses on those days — they are simply not shown. Nothing is faked.
        """
        a = build()

        assert a.window_start == date(2026, 3, 2)  # 60 days after the 2026-01-01 start
        assert len(a.days) == 90
        assert not any(r.decision.has(ReasonCode.INSUFFICIENT_HISTORY) for r in a.days)

    def test_the_same_spec_and_seed_produce_the_same_bytes(self) -> None:
        """Two deploys a week apart must show the same household doing the same things."""
        assert art.to_json(build()) == art.to_json(build())

    def test_a_sweep_shows_up_on_the_card_the_next_day(self) -> None:
        """T+1 settlement: yesterday's sweep is what lands on today's ledger."""
        a = build()
        by_day = {r.day: r for r in a.days}

        swept = next(
            r for r in a.days[:-1] if r.decision.action is Action.SWEEP and r.decision.amount > 0
        )
        tomorrow = by_day[swept.day + timedelta(days=1)]

        # The card also accrues a day of interest and may take the household's own payment,
        # so the drop is not exactly the sweep — but a $400 sweep dwarfs a day of interest
        # on this balance, and the balance must fall.
        assert tomorrow.debt_balance < swept.debt_balance

    def test_sweeps_leave_the_checking_account(self) -> None:
        """`sim/` knows nothing of our sweeps. If we do not subtract them, the engine keeps
        finding surplus that, in the world it just created, is already spent.

        Checked day-over-day: the change in the household's checking balance is whatever the
        household itself did that day, *minus* the sweep that settled from the day before.
        """
        a = build()
        history = generate(DEMO_SPEC, start=date(2026, 1, 1), days=150, seed=7)
        swept_days = [r for r in a.days[:-1] if r.decision.action is Action.SWEEP]

        assert swept_days  # otherwise this asserts nothing
        by_day = {r.day: r for r in a.days}

        for record in swept_days:
            tomorrow = by_day[record.day + timedelta(days=1)]
            household_did = history.balance_on(tomorrow.day) - history.balance_on(record.day)

            assert tomorrow.checking_balance == money(
                record.checking_balance + household_did - record.decision.amount
            )

    def test_the_weekly_cap_binds_when_sweeps_stack_up(self) -> None:
        """`Snapshot.swept_this_week` defaults to zero, so a walk that forgot to track it
        would silently never bind WEEKLY_CAP — the cap would look enforced and never be.

        Note the explicit `min_days_between_sweeps=0`. Under the demo's own weekly cadence the
        household gets one sweep a week, so a *weekly* cap has nothing to stack against and can
        never bind — the cadence subsumes it. WEEKLY_CAP is not dead code (a household spacing
        sweeps every 3 days with a high per-sweep cap still reaches it), but it is no longer
        reachable at the shipped policy, and this test pins the walk's bookkeeping rather than
        pretending otherwise.
        """
        tight = UserPolicy(
            buffer_floor=money("800.00"),
            max_sweep=money("400.00"),
            max_weekly_sweep=money("500.00"),  # a second full sweep cannot fit
            min_days_between_sweeps=0,  # the daily engine — the only cadence the cap can bind under
        )
        a = build(policy=tight)

        assert any(r.decision.has(ReasonCode.WEEKLY_CAP) for r in a.days)

    def test_the_cadence_holds_the_engine_off_between_sweeps(self) -> None:
        """The demo's own policy spaces sweeps a week apart — so the feed must show it."""
        a = build()

        swept = [r.day for r in a.days if r.decision.action is Action.SWEEP]
        gaps = [(b - x).days for x, b in zip(swept, swept[1:], strict=False)]

        assert swept, "the served window has no sweep to space"
        assert all(g >= 7 for g in gaps), f"sweeps landed closer than the cadence allows: {gaps}"
        assert any(r.decision.has(ReasonCode.CADENCE_HOLD) for r in a.days)

    def test_a_cadence_hold_still_carries_a_projection(self) -> None:
        """Daily data, weekly money — the days we hold are still measured.

        A hold that arrived as a *blocking* refusal would carry no projected low, and
        `engine/outcome.py:grade()` raises on those. Six days in seven would drop out of the
        calibration record for a reason that has nothing to do with forecasting.
        """
        a = build()
        held = [r for r in a.days if r.decision.has(ReasonCode.CADENCE_HOLD)]

        assert held
        assert all(r.decision.projected_low_balance is not None for r in held)
        assert all(r.decision.has(ReasonCode.PROJECTION) for r in held)

    def test_the_idle_savings_advisory_is_surfaced(self) -> None:
        a = build()
        assert any(r.decision.has(ReasonCode.IDLE_CASH_ELSEWHERE) for r in a.days)
        assert all(r.savings_balance == SAVINGS_BALANCE for r in a.days)

    def test_a_card_paid_off_mid_window_keeps_being_served(self) -> None:
        """ "Paid off" is a REFUSE carrying NO_DEBT, never a third Action — and those days
        stay in the feed rather than vanishing from it."""
        # Big enough to survive the household's own two card payments during the warm-up
        # (a smaller card clears before the served window even opens, and then there is no
        # sweep left to show), small enough that the sweeps finish it off inside the window.
        #
        # Raised from $2,400 when the cadence went weekly: one sweep a week at a $1,600 cap
        # clears a $2,400 card during the *warm-up*, so the served window opened on an
        # already-dead card and the build failed for want of a single SWEEP. $9,000 leaves
        # ~8 sweeps of runway and ~27 paid-off days, so it is not perched on either cliff —
        # and unlike the old figure it sits inside prd.md §3's $8-40k persona band.
        #
        # `card_share=0` because "paid off" has to *stay* paid off to be worth asserting. A
        # household that keeps charging the card drives the balance to zero and then straight
        # back up again on the next coffee — which is correct, and is exactly why a card in
        # active use is never durably "paid off". That is a different scenario than this one.
        a = build(
            spec=spec_with(
                balance=money("9000.00"),
                minimum_payment=money("50.00"),
                spend=SpendSpec(
                    zero_day_probability=0.25,
                    median=money("38.00"),
                    log_sigma=0.9,
                    card_share=0.0,
                ),
            )
        )

        cleared = [r for r in a.days if r.paid_off]
        assert cleared
        assert all(r.decision.action is Action.REFUSE for r in cleared)
        assert all(r.debt_balance == Decimal(0) for r in cleared)
        assert a.summary.paid_off
        assert a.summary.targeted_debt_id is None

    def test_a_one_sided_window_fails_the_build(self) -> None:
        """A demo that only ever sweeps, or only ever refuses, is not worth deploying — and
        this is where that is caught, rather than in front of an interviewer."""
        never_sweeps = UserPolicy(
            buffer_floor=money("100000.00"),  # nothing is ever spare
            max_sweep=money("400.00"),
            max_weekly_sweep=money("1600.00"),
        )

        with pytest.raises(ValueError, match="no SWEEP"):
            build(policy=never_sweeps)


class TestTheArtifact:
    def test_a_decimal_survives_the_round_trip_exactly(self) -> None:
        """Not "to within a cent" — exactly. A float in this path is a silently wrong cent."""
        a = build()
        back = art.from_json(art.to_json(a))

        assert back == a
        assert back.summary.interest_avoided_total == a.summary.interest_avoided_total
        for before, after in zip(a.days, back.days, strict=True):
            assert after.decision == before.decision
            assert after.debts == before.debts
            # A rate, not money: quantizing 0.2399 to cents would turn 23.99% into 24%.
            assert after.debts[0].apr == before.debts[0].apr == Decimal("0.2399")

    def test_reason_params_keep_their_types(self) -> None:
        """`Reason.params` is an open mapping of Decimals, dates and ints. Untagged JSON
        would flatten every one of them to a string or a float."""
        a = build()
        back = art.from_json(art.to_json(a))

        projection = next(
            r
            for record in back.days
            for r in record.decision.reasons
            if r.code is ReasonCode.PROJECTION
        )

        assert isinstance(projection.params["low"], Decimal)
        assert isinstance(projection.params["low_day"], date)
        assert isinstance(projection.params["buffer"], Decimal)

    def test_the_summary_sums_the_engines_own_claims(self) -> None:
        """Interest avoided is never recomputed here — it is summed from the reasons the
        engine chose to emit, so the discipline in `engine/interest.py` about when a claim
        can honestly be made is inherited rather than reimplemented."""
        a = build()

        expected = sum(
            (
                r.params["amount"]
                for record in a.days
                for r in record.decision.reasons
                if r.code is ReasonCode.INTEREST_AVOIDED
            ),
            start=money("0.00"),
        )

        assert a.summary.interest_avoided_total == expected
        assert a.summary.sweep_count + a.summary.refuse_count == len(a.days)

    def test_a_hand_edited_amount_is_rejected(self) -> None:
        a = build()
        raw = a.to_dict()
        raw["days"][0]["decision"]["amount"] = {"$dec": "12.345"}  # not cents

        with pytest.raises(art.ArtifactError, match="not quantized"):
            art.Artifact.from_dict(raw)

    def test_a_truncated_window_is_rejected(self) -> None:
        a = build()
        raw = a.to_dict()
        raw["days"] = raw["days"][:-1]  # window end no longer matches the last day

        with pytest.raises(art.ArtifactError, match="does not match"):
            art.Artifact.from_dict(raw)

    def test_garbage_is_rejected_as_garbage(self) -> None:
        with pytest.raises(art.ArtifactError, match="not valid JSON"):
            art.from_json("{not json")

    def test_the_committed_artifact_is_the_one_the_code_generates(self) -> None:
        """The artifact is committed rather than built at deploy time, so it can go stale
        against the spec. This is the check that says so, and it runs in CI."""
        assert art.DEFAULT_PATH.read_text() == art.to_json(build()), (
            "backend/data/decisions.json is stale — re-run `python -m backend.precompute`"
        )


class TestDerivation:
    """Raw history -> the card types. Pure derivation: nothing here decides anything.

    The two properties that matter are both about *not* freeing up money we shouldn't:
    behavior is UNKNOWN until we have really seen three cycles, and an uncertain calendar
    reserves early rather than late.
    """

    def charging_spec(self, **kw: object) -> HouseholdSpec:
        """A household that actually uses its card. Tests mutate one thing at a time."""
        base: dict[str, object] = {
            "opening_balance": money("4000.00"),
            "payroll": PayrollSpec(
                net_pay=money("2600.00"),
                cadence=PayCadence.BIWEEKLY,
                first_payday=date(2026, 1, 2),
                variation=Decimal("0.00"),
            ),
            "bills": (BillSpec(label="rent", day_of_month=1, mean=money("1500.00")),),
            "spend": SpendSpec(0.0, money("40.00"), 0.0, card_share=1.0),
            "cards": (
                CardSpec(
                    balance=ZERO,
                    apr=Decimal("0.1899"),
                    minimum_payment=money("40.00"),
                    payment=money("600.00"),
                    payment_day_of_month=20,
                    close_day_of_month=20,
                    behavior=PaymentBehavior.TRANSACTOR,
                ),
            ),
        }
        base.update(kw)
        return HouseholdSpec(**base)  # type: ignore[arg-type]

    def test_behavior_is_unknown_below_three_observed_cycles(self) -> None:
        """We refuse rather than guess. The guess is load-bearing twice: it sets the reserve
        *and* it decides whether we may claim to have saved them anything."""
        spec = self.charging_spec()
        h = generate(spec, WINDOW_START, days=45, seed=5)  # ~1 payment

        behavior, cycles = classify_behavior(h, spec.cards[0], WINDOW_START + timedelta(days=44))
        assert behavior is PaymentBehavior.UNKNOWN
        assert cycles < 3

    def test_a_household_that_clears_every_statement_is_a_transactor(self) -> None:
        spec = self.charging_spec()
        h = generate(spec, WINDOW_START, days=130, seed=5)

        behavior, _ = classify_behavior(h, spec.cards[0], WINDOW_START + timedelta(days=129))
        assert behavior is PaymentBehavior.TRANSACTOR

    def test_a_household_carrying_a_balance_two_cycles_running_is_a_revolver(self) -> None:
        """A transactor who misses a payment has silently lost their grace period and is
        accruing at the full APR *today*. Waiting is the expensive direction."""
        spec = self.charging_spec(
            cards=(
                CardSpec(
                    balance=money("5000.00"),  # they carry, and never clear it
                    apr=Decimal("0.2399"),
                    minimum_payment=money("100.00"),
                    payment=money("300.00"),
                    payment_day_of_month=20,
                    close_day_of_month=20,
                    behavior=PaymentBehavior.REVOLVER,
                ),
            ),
        )
        h = generate(spec, WINDOW_START, days=130, seed=5)

        behavior, _ = classify_behavior(h, spec.cards[0], WINDOW_START + timedelta(days=129))
        assert behavior in (PaymentBehavior.REVOLVER, PaymentBehavior.MINIMUM_ONLY)
        assert behavior is not PaymentBehavior.TRANSACTOR

    def test_the_observed_payment_comes_from_payments_not_from_inferred_cycles(self) -> None:
        """This figure is payments-divided-by-cycles and it feeds the REVOLVER reserve.

        An inference that invents more, shorter cycles would divide the same payments across a
        bigger denominator and pull it *down* — shrinking the very reserve the inference was
        supposed to protect. So: count the payments. Do not model the calendar.
        """
        spec = self.charging_spec(
            cards=(
                CardSpec(
                    balance=money("9000.00"),
                    apr=Decimal("0.2399"),
                    minimum_payment=money("180.00"),
                    payment=money("400.00"),
                    payment_day_of_month=20,
                    behavior=PaymentBehavior.REVOLVER,
                ),
            ),
        )
        h = generate(spec, WINDOW_START, days=130, seed=5)
        observed = observed_monthly_payment(h, spec.cards[0], WINDOW_START + timedelta(days=129))

        # They pay $400 every cycle. Not the $180 minimum, and not a diluted fraction of it.
        assert observed == money("400.00")

    def test_no_observed_payment_yields_no_claim_rather_than_the_minimum(self) -> None:
        """Falling back to the minimum is the flattering assumption prd.md §5.1 bans."""
        spec = self.charging_spec()
        h = generate(spec, WINDOW_START, days=30, seed=5)

        assert observed_monthly_payment(h, spec.cards[0], WINDOW_START + timedelta(days=29)) is None

    def test_an_uncertain_close_date_reserves_early_never_late(self) -> None:
        """Getting the close wrong by a day moves a month of spend across the horizon boundary.

        Wrong-and-early costs a smaller sweep. Wrong-and-late is an overdraft. Only one of
        those is survivable, so the inference is only ever allowed to move the obligation
        toward us.
        """
        spec = self.charging_spec()
        today = WINDOW_START + timedelta(days=20)
        h = generate(spec, WINDOW_START, days=21, seed=5)

        _day, certain = infer_close_day(h, spec.cards[0], today)
        assert not certain, "one payment is not a cycle — we cannot be sure"

        card = derive_card(h, spec.cards[0], today, ledger_balance=money("1000.00"))
        # The due date is pulled to the near edge, inside the horizon, rather than drifting out.
        assert card.statement_due_date <= today + timedelta(days=30)

    def test_a_card_carries_both_the_closed_statement_and_the_unbilled_charges(self) -> None:
        """The field that the first draft of the design forgot, and the reason the reserve
        dropped to $0 for a third of every cycle."""
        spec = self.charging_spec()
        # Two days past the close: a statement has closed, and new charges are already landing.
        today = date(2026, 2, 22)
        h = generate(spec, WINDOW_START, days=(today - WINDOW_START).days + 1, seed=5)

        card = derive_card(h, spec.cards[0], today, ledger_balance=money("1200.00"))

        assert card.unbilled_balance > ZERO, "charges since the close are not yet billed"
        assert card.next_close_date > today
        assert card.total_owed == card.statement_balance + card.unbilled_balance

    def test_an_unmatched_card_payment_is_detected(self) -> None:
        """A recurring $300 to CHASE CARD SVC with no Chase card connected is evidence of a
        liability we are not reserving against. It is the real coverage gate."""
        spec = self.charging_spec()
        h = generate(spec, WINDOW_START, days=130, seed=5)

        ghost = tuple(
            Txn(
                day=date(2026, m, 14),
                amount=money("-300.00"),
                label="CHASE CARD SVC",
                kind=TxnKind.CARD_PAYMENT,
                card_id="a-card-we-cannot-see",
            )
            for m in (1, 2, 3, 4)
        )
        haunted = History(
            spec=h.spec,
            start=h.start,
            days=h.days,
            opening_balance=h.opening_balance,
            txns=tuple(sorted(h.txns + ghost, key=lambda t: t.day)),
        )

        found = detect_unmatched_payments(haunted, {"card-1"}, WINDOW_START + timedelta(days=129))
        assert len(found) == 1
        assert found[0].merchant == "CHASE CARD SVC"
        assert found[0].typical_amount == money("300.00")
        assert found[0].months_observed >= 3

    def test_a_payment_to_a_card_we_can_see_is_not_unmatched(self) -> None:
        spec = self.charging_spec()
        h = generate(spec, WINDOW_START, days=130, seed=5)

        found = detect_unmatched_payments(h, {"card-1"}, WINDOW_START + timedelta(days=129))
        assert found == ()

    def test_a_one_off_card_shaped_transfer_does_not_trip_the_detector(self) -> None:
        """It happened once. That is not a liability, it is a transfer. Recurrence is required."""
        spec = self.charging_spec()
        h = generate(spec, WINDOW_START, days=130, seed=5)

        once = (
            Txn(
                day=date(2026, 2, 14),
                amount=money("-300.00"),
                label="CHASE CARD SVC",
                kind=TxnKind.CARD_PAYMENT,
                card_id="ghost",
            ),
        )
        haunted = History(
            spec=h.spec,
            start=h.start,
            days=h.days,
            opening_balance=h.opening_balance,
            txns=tuple(sorted(h.txns + once, key=lambda t: t.day)),
        )

        assert detect_unmatched_payments(haunted, {"card-1"}, date(2026, 5, 1)) == ()

    def test_coverage_is_unmatched_when_evidence_exists_and_unattested_otherwise(self) -> None:
        spec = self.charging_spec()
        h = generate(spec, WINDOW_START, days=130, seed=5)
        today = WINDOW_START + timedelta(days=129)
        card = derive_card(h, spec.cards[0], today, ledger_balance=money("500.00"))

        unattested = derive_portfolio(h, (card,), today, attested=False)
        assert unattested.coverage is CoverageState.UNATTESTED
        assert not unattested.is_complete

        attested = derive_portfolio(h, (card,), today, attested=True)
        assert attested.coverage is CoverageState.COMPLETE
        assert attested.is_complete

    def test_the_spend_profile_reads_its_worst_window_off_the_households_own_history(self) -> None:
        """Non-parametric by construction. The learning is explicit: variance grows with
        sqrt(t), and the current model grows it with t."""
        spec = self.charging_spec(spend=SpendSpec(0.0, money("40.00"), 0.0, card_share=0.0))
        h = generate(spec, WINDOW_START, days=130, seed=5)

        profile = derive_spend_profile(h, WINDOW_START + timedelta(days=129))

        assert profile.rolling_30d_cash, "a 130-day history has overlapping 30-day windows"
        # ~$40/day on the card-free spec => a 30-day window near $1,200.
        assert money("1000.00") < profile.worst_30d_cash < money("1500.00")
        assert profile.worst_30d_card == ZERO

    def test_spend_that_moved_to_the_card_shows_up_in_the_card_series_not_the_cash_one(
        self,
    ) -> None:
        """The collapse that makes the whole feature urgent, now visible in the profile."""
        spec = self.charging_spec()  # card_share=1.0
        h = generate(spec, WINDOW_START, days=130, seed=5)

        profile = derive_spend_profile(h, WINDOW_START + timedelta(days=129))

        assert profile.worst_30d_cash == ZERO
        assert profile.worst_30d_card > ZERO

    def test_derivation_changes_no_decision(self) -> None:
        """U3 is derivation only. The artifact must be exactly what it was."""
        assert art.DEFAULT_PATH.read_text() == art.to_json(build())


class TestTheSpendSnapshot:
    """Serialization of the spend surface. `SCHEMA_VERSION` bumped to 2."""

    def test_the_spend_snapshot_round_trips_without_losing_a_cent(self) -> None:
        """Money crosses the wire as strings, never JSON numbers. A float here is a rounding
        bug with a very long fuse — it surfaces as a cent of disagreement between the dashboard
        and the KPI, months later, with no obvious cause."""
        original = build()
        restored = art.from_json(art.to_json(original))

        assert restored.spend == original.spend
        assert restored.spend.statement_balance == original.spend.statement_balance
        assert restored.spend.rolling_30d_cash == original.spend.rolling_30d_cash

    def test_the_committed_artifact_carries_the_spend_surface(self) -> None:
        loaded = art.load()
        assert loaded.version == art.SCHEMA_VERSION
        assert loaded.spend.rolling_30d_cash, "the strip chart has no data"

    def test_the_unbilled_statement_is_due_after_the_closed_one(self) -> None:
        """They are a month apart, and that gap is the whole reason card spend is an engine
        input: two charges three weeks apart leave checking a month apart."""
        spend = build().spend
        assert spend.unbilled_due > spend.statement_due

    def test_the_spend_surface_feeds_no_decision(self) -> None:
        """U6 is comprehension. If `forecast.py` ever reads the rolling series, that is the
        change the 2026-07-13 learning says needs a measured breach rate first — and it does not
        get to arrive quietly inside a dashboard ticket.
        """
        import engine.forecast as forecast

        with open(forecast.__file__) as fh:
            body = fh.read()

        assert "rolling_30d" not in body
        assert "SpendProfile" not in body


class TestTodayIsNotForecastTwice:
    """The payday double-count. A live bug, and an expensive one.

    `Snapshot.accounts[].balance` is the balance at the **end of today**, so everything that
    happened today is already inside it. `derive_cash_events` used to emit today's events as
    *future* ones as well — so on a payday the forecast counted that paycheck twice.

    A $2,600 phantom inflow. The engine projected a low thousands of dollars too high, swept
    against money that was never there, and overdrew the household. Across a 60-household
    population it caused **43 sweep-caused overdrafts** — `prd.md` §5.2's guardrail, breached —
    and the single-household demo showed **zero**, because whether it bites depends on the cash
    position on whichever paydays a given seed happens to produce.

    It is the exact inverse of the rule the forecast is built on: money arrives **late and
    small**. Counting a paycheck that has already landed as though it were still coming makes it
    arrive *twice*.
    """

    def test_no_event_is_emitted_for_today(self) -> None:
        payday = date(2026, 1, 2)  # DEMO_SPEC's first payday
        events = derive_cash_events(DEMO_SPEC, payday)

        assert events, "the horizon should still hold future events"
        assert all(e.expected_date > payday for e in events), (
            "an event on `today` is already in the balance — emitting it again forecasts it twice"
        )

    def test_a_payday_today_is_not_counted_as_future_income(self) -> None:
        payday = date(2026, 1, 2)
        payroll = [e for e in derive_cash_events(DEMO_SPEC, payday) if e.label == "payroll"]

        assert payroll, "later paydays are still forecast"
        assert all(e.expected_date > payday for e in payroll)

    def test_a_bill_due_today_is_not_counted_as_a_future_outflow(self) -> None:
        """The same bug in the safe direction — but a *phantom* outflow is still a phantom.

        It would make the engine refuse for money that had already left, which is a smaller sin
        than overdrafting someone and still a lie about their balance.
        """
        rent_day = date(2026, 2, 1)  # DEMO_SPEC's rent falls on the 1st
        rent = [e for e in derive_cash_events(DEMO_SPEC, rent_day) if e.label == "rent"]

        assert all(e.expected_date > rent_day for e in rent)


class TestGenerateIsNotPrefixStable:
    """**Documents a known defect.** Found while unifying the walk (ticket `0019`); not fixed there.

    `(spec, seed, days=150)` and `(spec, seed, days=181)` are **different households**, not two
    windows onto one. `sim.household.generate()` draws from a single RNG stream in order — payroll,
    then bills, then discretionary — and the payroll and bill loops both run to
    `through = start + days - 1`. So the number of draws taken *before* the spend loop depends on
    `days`, and every discretionary draw shifts with it.

    `generate()`'s own docstring — *"Deterministic in `(spec, start, days, seed)`"* — is true, and
    is the trap. `days` is part of the household's **identity**, not a window onto it.

    **The consequence: `build()` walks `WARMUP + SERVED` = 150 days and `replay()` walks
    `+ HORIZON_DAYS + 1` = 181, so they have never walked the same household.** That is ticket
    `0019`'s own thesis — the harness must grade the engine that ships — one level deeper than the
    dial it was written about.

    What survives and what does not:

    - **`calibrate.py`'s population statistics survive.** 20 arbitrary seeds per shape are still 20
      valid households drawn from the same generator, so 2.3% / 0-in-590 / ~$544K remain honest
      statements about a synthetic population.
    - **Per-household claims tying a replay number to the shipped artifact do not.**
      `docs/plans/2026-07-14-001`'s "6.9% breach **on the demo household**" describes a household
      `backend/data/decisions.json` has never contained.

    Not fixed in `0019` because the fix regenerates the artifact and moves every measured number in
    the repo — which is exactly the change `0019`'s byte-identical test exists to refuse. It needs
    its own ticket, its own measurement, and its own diff.
    """

    def test_the_same_seed_and_a_longer_window_are_different_households(self) -> None:
        """If this fails, someone has made `generate()` prefix-stable — which is **the fix**.

        Do not delete this test to make it pass. Delete this *class*, and re-measure everything its
        docstring warns about: the committed artifact, `calibrate.py`'s numbers, and every figure
        those are cited in across `prd.md`, `strategy.md`, and `decision-engine.md`.
        """
        cutoff = WINDOW_START + timedelta(days=149)
        short = generate(DEMO_SPEC, WINDOW_START, 150, seed=SEED)
        long = generate(DEMO_SPEC, WINDOW_START, 181, seed=SEED)

        def prefix(h: History) -> list[tuple[date, TxnKind, Decimal]]:
            return [(t.day, t.kind, t.amount) for t in h.txns if t.day <= cutoff]

        assert prefix(short) != prefix(long), (
            "generate() is now prefix-stable. This is the fix, not a regression -- see this "
            "class's docstring, delete it, and re-measure the artifact and the calibration."
        )

    def test_build_and_replay_therefore_walk_different_households(self) -> None:
        """The consequence, in the two callers that matter, stated concretely."""
        total = WARMUP_DAYS + SERVED_DAYS
        as_build_sees_it = generate(DEMO_SPEC, WINDOW_START, total, seed=SEED)
        as_replay_sees_it = generate(DEMO_SPEC, WINDOW_START, total + HORIZON_DAYS + 1, seed=SEED)

        same_day = WINDOW_START + timedelta(days=100)
        assert as_build_sees_it.balance_on(same_day) != as_replay_sees_it.balance_on(same_day), (
            "build() and replay() now agree on the household -- if generate() was fixed, see "
            "TestGenerateIsNotPrefixStable's docstring and re-measure."
        )


class TestTheSpendDial:
    """`spend_30d_high` — the empirical spend model, shipped **inert**.

    `SPEND_QUANTILE = None` means `forecast.py` falls back to `daily_discretionary_high`, which
    is today's behaviour exactly. The structure ships; the loosening does not — the measurement
    refused it. See `backend/calibrate.py` and
    `docs/learnings/2026-07-14-the-empirical-spend-model-is-not-a-drop-in.md`.
    """

    def test_the_dial_is_off_and_the_engine_behaves_exactly_as_it_did(self) -> None:
        """If this ever fails, someone has loosened the forecast — and that needs a measurement,
        not a commit."""
        from backend.precompute import SPEND_QUANTILE

        assert SPEND_QUANTILE is None

    def test_the_dial_reaches_the_artifact_that_ships(self) -> None:
        """The **wiring**, not the setting — and the reason ticket 0019 exists.

        `calibrate.py` sweeps this dial through `replay()` → `assemble_snapshot()`. `build()`
        must read the same one, or the harness grades an engine that never shipped.

        Before 0019 it could not: `build()` had no `spend_quantile` parameter at all. It
        constructed its `Snapshot` inline and never set `spend_30d_high`, so the field defaulted
        to `None` on every artifact day regardless of the dial. Harmless only because
        `SPEND_QUANTILE` is `None` and both paths agreed by accident. The day the dial moved —
        which is the only reason `calibrate.py` exists — it would have licensed a forecast the
        artifact was structurally incapable of producing.

        `decision-engine.md` §6.6's test guards the dial's *setting*. This one guards its *wiring*.
        """
        off = art.to_json(build(spend_quantile=None))
        on = art.to_json(build(spend_quantile=0.99))

        assert off == art.to_json(build()), "`None` is, and stays, the shipped default"
        assert off != on, (
            "the dial does not reach the artifact — `build()` is ignoring `spend_quantile`, "
            "so `calibrate.py` is measuring a forecast that never ships"
        )

    def test_with_the_dial_off_no_snapshot_carries_an_empirical_reserve(self) -> None:
        history = generate(DEMO_SPEC, WINDOW_START, 120, seed=7)
        snapshot = assemble_snapshot(
            history=history,
            today=WINDOW_START + timedelta(days=90),
            spec=DEMO_SPEC,
            policy=DEMO_POLICY,
            ledger_balances={DEMO_SPEC.card.card_id: money("9000.00")},
            checking=money("3000.00"),
        )
        assert snapshot.spend_30d_high is None

    def test_the_dial_reads_the_households_own_worst_window(self) -> None:
        """Non-parametric: no distributional assumption, their real skew and autocorrelation."""
        history = generate(DEMO_SPEC, WINDOW_START, 200, seed=7)
        today = WINDOW_START + timedelta(days=180)

        worst = spend_30d_high(history, today, quantile=1.0)
        median = spend_30d_high(history, today, quantile=0.5)

        assert worst is not None and median is not None
        assert worst > median, "the worst window is worse than the median one"

    def test_a_quantile_above_one_scales_past_their_own_history(self) -> None:
        """The only way an empirical model can be as conservative as the parametric one it
        replaces — see the calibration. Their worst month is not the worst month they can have."""
        history = generate(DEMO_SPEC, WINDOW_START, 200, seed=7)
        today = WINDOW_START + timedelta(days=180)

        worst = spend_30d_high(history, today, quantile=1.0)
        scaled = spend_30d_high(history, today, quantile=1.5)

        assert worst is not None and scaled is not None
        assert scaled == money(worst * Decimal("1.5"))


class TestTheWalkCarriesAPortfolio:
    """Ticket `0027`. **The walk could not simulate a multi-card household.**

    It built one `DebtLedger` from `spec.card` and handed that single balance to `derive_card` for
    every card, so a $3,000 card reported the $14,000 card's balance. `_select_target` would rank
    them equal and pick on APR alone; the obligation reserve would count the same $14,009 twice.

    It survived because the engine's multi-card types, reserve and ranking (`0010`–`0017`) are
    built and tested, and **every household the walk has ever driven has one card** — so nothing
    exercised them. `HouseholdSpec.card`'s own docstring warns against exactly the read the walk
    was doing.
    """

    @staticmethod
    def _two_cards() -> HouseholdSpec:
        """$14,000 at 23.99% and $3,000 at 18.99%. Different balances, so a shared ledger shows."""
        big, small = DEMO_SPEC.cards[0], DEMO_SPEC.cards[0]
        return replace(
            DEMO_SPEC,
            cards=(
                replace(big, card_id="card_big", balance=money("14000.00"), apr=Decimal("0.2399")),
                replace(
                    small,
                    card_id="card_small",
                    balance=money("3000.00"),
                    apr=Decimal("0.1899"),
                    minimum_payment=money("60.00"),
                    payment=money("90.00"),
                ),
            ),
        )

    def test_each_card_reports_its_own_balance(self) -> None:
        """The defect, stated directly. Fails on pre-0027 code with 14009.20 == 14009.20."""
        spec = self._two_cards()
        history = generate(spec, WINDOW_START, 150, seed=SEED)
        day = next(iter(walk(history, spec, WINDOW_START, 150)))

        owed = {
            c.card_id: c.statement_balance + c.unbilled_balance
            for c in day.snapshot.portfolio.cards
        }

        assert owed["card_big"] != owed["card_small"], (
            "both cards report the same balance — the walk is sharing one ledger across the "
            "portfolio, so the engine is reasoning about a household that does not exist"
        )
        assert owed["card_big"] > money("13000.00")
        assert owed["card_small"] < money("4000.00")

    def test_a_sweep_settles_against_the_card_it_targeted(self) -> None:
        """`Decision.target_debt_id` names one card. The money must land on that one and no other.

        Paying the wrong card is not a rounding error: `interest.py` computes what the sweep saved
        from the *targeted* card's APR, so a sweep credited to the wrong ledger makes the claim in
        `prd.md` §1 — "that's $31 of interest you won't pay" — a number about a different debt.
        """
        spec = self._two_cards()
        history = generate(spec, WINDOW_START, 150, seed=SEED)

        prev: dict[str, Decimal] | None = None
        for w in walk(history, spec, WINDOW_START, 150):
            if prev is not None and w.swept_yesterday is not None:
                amount, target = w.swept_yesterday
                others = [cid for cid in prev if cid != target]
                # The targeted card fell by at least the sweep, net of the day's accrual.
                assert w.debt_balances[target] < prev[target], (
                    f"{w.day}: swept {amount} at {target}, but its balance did not fall"
                )
                for cid in others:
                    # An untargeted card only ever moves by its own accrual or its own payment —
                    # never by our sweep. It must not fall by the swept amount.
                    assert w.debt_balances[cid] >= prev[cid] - amount + money("0.01") or (
                        w.debt_balances[cid] > prev[cid] - amount
                    ), f"{w.day}: swept {amount} at {target} but {cid} moved like it was paid"
            prev = dict(w.debt_balances)

    def test_two_cards_paid_on_the_same_day_both_land(self) -> None:
        """`card_payments` was `{t.day: -t.amount}` — keyed on the day alone, so a second card paid
        on the same day silently overwrote the first. Both cards here pay on the 20th."""
        spec = self._two_cards()
        assert spec.cards[0].payment_day_of_month == spec.cards[1].payment_day_of_month, (
            "this test is meaningless unless both cards pay on the same day"
        )

        history = generate(spec, WINDOW_START, 150, seed=SEED)
        paid = {c: ZERO for c in ("card_big", "card_small")}
        for t in history.txns:
            if t.kind is TxnKind.CARD_PAYMENT and t.card_id in paid:
                paid[t.card_id] += -t.amount

        assert paid["card_big"] > ZERO and paid["card_small"] > ZERO, "the sim paid only one card"

        # Both cards' payments must reach their own ledgers: each balance ends below its opening.
        days = list(walk(history, spec, WINDOW_START, 150))
        assert days[-1].debt_balances["card_small"] < money("3000.00"), (
            "card_small never received its payments — they were keyed by day and overwritten"
        )

    def test_each_card_posts_interest_on_its_own_close_day(self) -> None:
        """`DebtLedger.accrue` posted when `day.day == STATEMENT_DAY`, the module constant — so
        every card posted on the 20th regardless of its own cycle."""
        a = DebtLedger(principal=money("1000.00"), apr=Decimal("0.24"), close_day=5)
        b = DebtLedger(principal=money("1000.00"), apr=Decimal("0.24"), close_day=20)

        for offset in range(40):
            day = date(2026, 1, 1) + timedelta(days=offset)
            a.accrue(day)
            b.accrue(day)
            if day == date(2026, 1, 5):
                assert a.accrued == ZERO, "card A did not post on its own close day"
                assert b.accrued > ZERO, "card B posted on A's close day"


class TestBuildServesAPortfolio:
    """Ticket `0030`. `build()` raised on a multi-card spec from `0027` until `DayRecord` grew the
    tuple the walk had carried all along.

    The guard was right for as long as it stood — the only alternatives were reporting `cards[0]`,
    which is the bug `0027` fixed one function up, or refusing. It refused. These tests are what
    replaces it, and they check the thing the guard was standing in for: that a portfolio arrives
    whole, each card with its own balance and its own rate.
    """

    def test_a_multi_card_spec_is_served_rather_than_refused(self) -> None:
        spec = TestTheWalkCarriesAPortfolio._two_cards()
        artifact = build(spec=spec)

        assert artifact.days
        for record in artifact.days:
            assert len(record.debts) == 2, f"{record.day}: a card went missing"

    def test_each_card_carries_its_own_balance(self) -> None:
        """0027 one level up: one ledger for a portfolio reported a $3,000 card's balance as
        $14,009.20. Two cards reporting one balance is that bug arriving in the artifact."""
        spec = TestTheWalkCarriesAPortfolio._two_cards()
        artifact = build(spec=spec)

        record = artifact.days[0]
        balances = {d.debt_id: d.balance for d in record.debts}
        assert len(set(balances.values())) == 2, f"cards share a balance: {balances}"

    def test_the_portfolio_total_is_the_sum_of_its_cards(self) -> None:
        spec = TestTheWalkCarriesAPortfolio._two_cards()
        artifact = build(spec=spec)

        for record in artifact.days:
            assert record.debt_balance == sum(d.balance for d in record.debts)

    def test_the_summary_targets_a_card_the_engine_actually_chose(self) -> None:
        """Never `max(apr)`. `_select_target` ranks only among cards it is honest to sweep to, so
        re-deriving the target from the rates alone would pick a transactor the engine refuses."""
        spec = TestTheWalkCarriesAPortfolio._two_cards()
        artifact = build(spec=spec)

        targeted = artifact.summary.targeted_debt_id
        if targeted is None:
            pytest.skip("this household never swept, so there is no target to check")

        chosen = {d.decision.target_debt_id for d in artifact.days if d.decision.target_debt_id}
        assert targeted in chosen, "the summary targets a card no decision ever aimed at"


class TestTheArtifactRecordsWhatTheEngineSaw:
    """Ticket `0030`, and it is the reason the APR half of `debts` is not optional.

    `build()` read `debt_apr=spec.card.apr` — and `spec` is `sim/`, the ground truth the engine is
    **not allowed to see**. On a card whose issuer does not report a rate, the engine decides
    against an estimated 23% while the spec knows the real 23.99%, and the artifact recorded the
    23.99%: `0028` inverted, a guess quietly upgraded to a fact on its way to the dashboard.

    It was invisible because `DEMO_SPEC` reports its rate, so the two agreed, and every household
    where they disagree was one `build()` refused to serve.
    """

    @staticmethod
    def _hidden_rate_spec() -> HouseholdSpec:
        hidden = replace(DEMO_SPEC.cards[0], apr_reported=False)
        return replace(DEMO_SPEC, cards=(hidden,))

    def test_an_unreported_rate_is_recorded_as_the_estimate_not_the_truth(self) -> None:
        spec = self._hidden_rate_spec()
        artifact = build(spec=spec)

        for record in artifact.days:
            debt = record.debts[0]
            assert debt.apr == ESTIMATED_APR, (
                f"{record.day}: the artifact reports {debt.apr}, which the engine never saw — "
                f"that is spec.card.apr ({spec.cards[0].apr}), read off the answer key"
            )
            assert debt.apr_source is AprSource.ESTIMATED

    def test_the_summary_does_not_leak_it_either(self) -> None:
        artifact = build(spec=self._hidden_rate_spec())
        assert artifact.summary.targeted_debt_apr in (None, ESTIMATED_APR)

    def test_a_reported_rate_is_still_the_reported_one(self) -> None:
        """The other half: the fix must not blanket everything with the estimate."""
        artifact = build(spec=DEMO_SPEC)

        for record in artifact.days:
            assert record.debts[0].apr == DEMO_SPEC.cards[0].apr
            assert record.debts[0].apr_source is AprSource.REPORTED


class TestTheAprEstimate:
    """Ticket `0028`. **Act on the estimate. Never bill for it.**

    `decision-engine.md` §6.3: Plaid does not report APR for many issuers, and the engine used to
    refuse to rank rather than guess. It now estimates at 23% — and the whole point of the
    provenance is that the two halves come apart:

    - `decide.py` **ranks** on the guess. A wrong target optimizes worse and overdraws nobody; no
      safety gate reads `apr_source`.
    - `interest.py` **refuses to price** it. `prd.md` §5.1's KPI and §1's "that's $31 of interest
      you won't pay" would otherwise be arithmetic on a number we invented.
    """

    @staticmethod
    def _unreported(**over) -> HouseholdSpec:
        """The demo household, whose issuer will not report the rate."""
        return replace(DEMO_SPEC, cards=(replace(DEMO_SPEC.cards[0], apr_reported=False, **over),))

    def test_an_unreported_card_is_estimated_and_says_so(self) -> None:
        spec = self._unreported()
        history = generate(spec, WINDOW_START, 150, seed=SEED)
        card = next(iter(walk(history, spec, WINDOW_START, 150))).snapshot.portfolio.cards[0]

        assert card.apr == ESTIMATED_APR == Decimal("0.23")
        assert card.apr_source is AprSource.ESTIMATED

    def test_a_reported_card_keeps_its_real_rate(self) -> None:
        """The demo card reports 23.99%. Nothing about 0028 may touch it — which is also why the
        committed artifact is byte-identical."""
        history = generate(DEMO_SPEC, WINDOW_START, 150, seed=SEED)
        card = next(iter(walk(history, DEMO_SPEC, WINDOW_START, 150))).snapshot.portfolio.cards[0]

        assert card.apr == Decimal("0.2399")
        assert card.apr_source is AprSource.REPORTED

    def test_the_engine_still_sweeps_on_an_estimate(self) -> None:
        """The point of estimating: the engine **acts** where APR_UNKNOWN used to refuse."""
        spec = self._unreported()
        history = generate(spec, WINDOW_START, 150, seed=SEED)
        days = list(walk(history, spec, WINDOW_START, 150))

        assert any(d.decision.action is Action.SWEEP for d in days), (
            "the engine refused all 150 days on an estimated card — it is meant to act"
        )
        assert not any(d.decision.has(ReasonCode.APR_UNKNOWN) for d in days), (
            "APR_UNKNOWN fired on a card we estimated"
        )

    def test_it_never_claims_a_saving_from_an_estimate(self) -> None:
        """**The half that must not be skipped.** The sweep happens and the feed says nothing about
        what it saved, because the rate is a guess.

        If this fails, `prd.md` §5.1's realized-interest-avoided — the number the company is graded
        on — has started reporting arithmetic on an invented APR.
        """
        spec = self._unreported()
        history = generate(spec, WINDOW_START, 150, seed=SEED)
        days = list(walk(history, spec, WINDOW_START, 150))
        swept = [d for d in days if d.decision.action is Action.SWEEP]

        assert swept, "no sweep to check"
        for d in swept:
            assert not d.decision.has(ReasonCode.INTEREST_AVOIDED), (
                f"{d.day}: claimed a saving computed from an estimated APR"
            )

    def test_a_reported_card_does_still_claim(self) -> None:
        """The control. Without this, the test above passes on an engine that never claims at
        all — which would be a different bug wearing the same green tick."""
        history = generate(DEMO_SPEC, WINDOW_START, 150, seed=SEED)
        swept = [
            d
            for d in walk(history, DEMO_SPEC, WINDOW_START, 150)
            if d.decision.action is Action.SWEEP
        ]
        assert swept, "no sweep to check"
        assert any(d.decision.has(ReasonCode.INTEREST_AVOIDED) for d in swept), (
            "a reported card stopped claiming its saving — 0028 broke the normal path"
        )

    def test_total_interest_refuses_an_estimate_directly(self) -> None:
        """At the unit, not through the walk: same card, same balance, only the provenance moves."""
        history = generate(DEMO_SPEC, WINDOW_START, 150, seed=SEED)
        card = derive_card(
            history,
            DEMO_SPEC.cards[0],
            WINDOW_START + timedelta(days=90),
            ledger_balance=money("9000.00"),
        )

        reported = replace(card, apr=Decimal("0.23"), apr_source=AprSource.REPORTED)
        estimated = replace(card, apr=Decimal("0.23"), apr_source=AprSource.ESTIMATED)

        assert total_interest(reported, WINDOW_START, money("450.00")) is not None
        assert total_interest(estimated, WINDOW_START, money("450.00")) is None

    def test_the_estimate_is_conservative_for_ranking(self) -> None:
        """23% sits near the bottom of the persona's 20-30% band (`prd.md` §3), so an estimated
        card loses to most cards we can actually price. That is the safe direction: we
        under-prioritize the card we cannot see rather than diverting money from one we can."""
        assert Decimal("0.25") > ESTIMATED_APR, (
            "the estimate has drifted above the middle of the persona's band — it now out-ranks "
            "cards we can price, which is the anti-conservative direction for a guess"
        )
        assert Decimal("0.20") <= ESTIMATED_APR, "below the persona's band entirely"

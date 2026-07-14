"""The day-by-day walk, and the artifact it writes.

The debt ledger and the rolling statistics are new stateful derivations with no precedent
in `engine/` or `sim/` to lean on, and every way they can be wrong is quiet: a balance that
drifts, a weekly cap that never binds, a percentile computed over the wrong window. So they
are pinned here directly, not just observed through a finished artifact.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from backend import artifact as art
from backend.precompute import (
    CHECKING_ID,
    DEMO_SPEC,
    SAVINGS_BALANCE,
    STATEMENT_DAY,
    WINDOW_START,
    DebtLedger,
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
)
from engine.models import (
    ZERO,
    Action,
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
    return HouseholdSpec(
        opening_balance=kw.pop("opening_balance", DEMO_SPEC.opening_balance),
        payroll=DEMO_SPEC.payroll,
        bills=DEMO_SPEC.bills,
        spend=DEMO_SPEC.spend,
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

    def test_the_card_payment_is_not_tagged_as_the_minimum(self) -> None:
        """Tagging the full $450 payment as DEBT_MINIMUM would hide $170 of real outflow.

        `forecast.py` skips DEBT_MINIMUM events entirely, and `decide()` reserves only
        `Debt.minimum_payment` ($280). The difference is money that genuinely leaves the
        account and would go uncounted — an under-count, which is the direction that ends in
        an overdraft.
        """
        events = derive_cash_events(DEMO_SPEC, date(2026, 3, 1))
        card = [e for e in events if e.label == "card payment"]

        assert card
        assert all(e.kind is EventKind.ORDINARY for e in card)
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
        a = build(spec=spec_with(balance=money("9000.00"), minimum_payment=money("50.00")))

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
            assert after.debt_apr == before.debt_apr == Decimal("0.2399")  # a rate, not money

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

"""The generator is ground truth for the shadow-mode harness. These tests are its spec.

Two properties carry everything downstream and are therefore tested hardest:

- **Determinism.** `(spec, seed)` must yield a byte-identical history forever. A backtest
  whose ground truth moves is not a backtest.
- **Point-in-time honesty.** `as_of(day)` must never leak a transaction the household could
  not have known about on `day`. Lookahead is the classic way a backtest reports a tail risk
  that is flattering and false, and `#4` builds its snapshots on this seam.

Everything else here is a distributional sanity check — enough to trust the households, not
so much that the suite becomes a statistics library.
"""

from __future__ import annotations

import statistics
from datetime import date, timedelta
from decimal import Decimal

import pytest

from engine.models import (
    ZERO,
    Account,
    ConnectionState,
    Debt,
    PaymentBehavior,
    Snapshot,
    UserPolicy,
    money,
)
from sim.household import (
    BillSpec,
    CardSpec,
    HouseholdSpec,
    PayCadence,
    PayrollSpec,
    ShockSpec,
    SpendSpec,
    TxnKind,
    generate,
)

START = date(2026, 1, 1)
DAYS = 180

CHECKING = "chk"


def spec(**kw) -> HouseholdSpec:
    """A stable, forecastable W2 household — the one the PRD §3 says we serve."""
    base = dict(
        opening_balance=money("4000.00"),
        payroll=PayrollSpec(
            net_pay=money("2600.00"),
            cadence=PayCadence.BIWEEKLY,
            first_payday=date(2026, 1, 2),
            variation=Decimal("0.02"),
        ),
        bills=(
            BillSpec(label="rent", day_of_month=1, mean=money("1800.00"), sd=money("0.00")),
            BillSpec(label="utilities", day_of_month=12, mean=money("140.00"), sd=money("35.00")),
        ),
        spend=SpendSpec(zero_day_probability=0.25, median=money("38.00"), log_sigma=0.9),
        cards=(
            CardSpec(
                balance=money("9000.00"),
                apr=Decimal("0.2399"),
                minimum_payment=money("180.00"),
                payment=money("400.00"),
                payment_day_of_month=20,
            ),
        ),
    )
    base.update(kw)
    return HouseholdSpec(**base)


def history(seed: int = 7, **kw):
    return generate(spec(**kw), start=START, days=DAYS, seed=seed)


class TestDeterminism:
    """A backtest whose ground truth moves is not a backtest."""

    def test_the_same_spec_and_seed_produce_an_identical_history(self) -> None:
        assert history(seed=7).txns == history(seed=7).txns

    def test_a_different_seed_produces_a_different_history(self) -> None:
        assert history(seed=7).txns != history(seed=8).txns

    def test_no_ambient_randomness_leaks_in(self) -> None:
        """Interleaving a global `random` call must not perturb the result.

        A module-level `random.*` call would make the history depend on whatever else ran
        first in the process — shared global state, and the end of reproducibility.
        """
        import random

        first = history(seed=7)
        random.random()
        random.random()
        second = history(seed=7)

        assert first.txns == second.txns


class TestMoneyDiscipline:
    def test_every_amount_is_a_decimal_quantized_to_cents(self) -> None:
        """The generator samples from continuous distributions. Not one float may escape.

        `money()` rejects floats at the engine boundary, so a float here would surface as a
        TypeError deep in a backtest run rather than at the point it was created.
        """
        for txn in history().txns:
            assert isinstance(txn.amount, Decimal)
            assert txn.amount == txn.amount.quantize(Decimal("0.01"))

    def test_a_generated_household_constructs_a_valid_snapshot_the_engine_accepts(self) -> None:
        """The round trip that matters: ground truth in, a real decision out."""
        from engine.decide import decide

        h = history()
        day = START + timedelta(days=90)

        snapshot = Snapshot(
            today=day,
            accounts=(
                Account(
                    account_id=CHECKING,
                    balance=h.balance_on(day),
                    connection=ConnectionState.HEALTHY,
                    balance_age_days=0,
                ),
            ),
            funding_account_id=CHECKING,
            events=(),
            pending=(),
            debts=(
                Debt(
                    debt_id="visa",
                    balance=money("9000.00"),
                    minimum_payment=money("180.00"),
                    minimum_due_date=day + timedelta(days=20),
                    apr=Decimal("0.2399"),
                ),
            ),
            policy=UserPolicy(
                buffer_floor=money("750.00"),
                max_sweep=money("300.00"),
                max_weekly_sweep=money("600.00"),
            ),
            daily_discretionary_high=money("100.00"),
            income_variation=0.05,
            history_days=90,
        )

        decide(snapshot)  # must not raise


class TestPointInTimeHonesty:
    """`as_of` is the seam #4's replay driver builds its snapshots on."""

    def test_as_of_never_exposes_a_transaction_from_the_future(self) -> None:
        day = START + timedelta(days=45)

        for txn in history().as_of(day).txns:
            assert txn.day <= day

    def test_as_of_is_a_strict_prefix_of_the_full_history(self) -> None:
        """Not merely a filter — the *same* transactions, unchanged.

        If `as_of` regenerated rather than sliced, a household's past would shift under the
        backtest exactly as Plaid's restatements shift it in production.
        """
        h = history()
        day = START + timedelta(days=45)
        sliced = h.as_of(day)

        assert sliced.txns == tuple(t for t in h.txns if t.day <= day)

    def test_the_balance_on_a_day_is_the_opening_balance_plus_everything_up_to_it(self) -> None:
        h = history()
        day = START + timedelta(days=45)

        expected = h.opening_balance + sum((t.amount for t in h.txns if t.day <= day), ZERO)

        assert h.balance_on(day) == expected


class TestPayroll:
    def test_biweekly_pay_lands_every_fourteen_days(self) -> None:
        paydays = [t.day for t in history().txns if t.kind is TxnKind.PAYROLL]

        assert paydays[0] == date(2026, 1, 2)
        gaps = {(b - a).days for a, b in zip(paydays, paydays[1:], strict=False)}
        assert gaps == {14}

    def test_semimonthly_pay_lands_twice_a_month(self) -> None:
        h = history(
            payroll=PayrollSpec(
                net_pay=money("2600.00"),
                cadence=PayCadence.SEMIMONTHLY,
                first_payday=date(2026, 1, 15),
                variation=Decimal("0.00"),
            )
        )
        january = [t.day.day for t in h.txns if t.kind is TxnKind.PAYROLL and t.day.month == 1]

        assert january == [15, 31]

    def test_pay_varies_around_the_stated_net(self) -> None:
        pay = [t.amount for t in history().txns if t.kind is TxnKind.PAYROLL]

        assert len(set(pay)) > 1  # not a constant
        assert abs(statistics.mean(pay) - money("2600.00")) < money("100.00")

    def test_zero_variation_pays_exactly_the_stated_net(self) -> None:
        h = history(
            payroll=PayrollSpec(
                net_pay=money("2600.00"),
                cadence=PayCadence.BIWEEKLY,
                first_payday=date(2026, 1, 2),
                variation=Decimal("0.00"),
            )
        )

        assert {t.amount for t in h.txns if t.kind is TxnKind.PAYROLL} == {money("2600.00")}


class TestDiscretionarySpend:
    """Real daily spend is zero-inflated and right-skewed. Nothing about it is Gaussian.

    This matters because the engine's `daily_discretionary_high` is a p90 of *this*
    distribution, and #4's replay draws its troughs from it. Model it as a normal and the
    entire calibration exercise is measuring the wrong tail.
    """

    def test_some_days_have_no_discretionary_spend_at_all(self) -> None:
        h = history()
        spend_days = {t.day for t in h.txns if t.kind is TxnKind.DISCRETIONARY}
        all_days = {START + timedelta(days=i) for i in range(DAYS)}

        assert all_days - spend_days  # at least one zero day

    def test_spend_is_right_skewed_so_the_mean_sits_above_the_median(self) -> None:
        amounts = [-t.amount for t in history().txns if t.kind is TxnKind.DISCRETIONARY]

        assert statistics.mean(amounts) > statistics.median(amounts)

    def test_discretionary_spend_is_always_an_outflow(self) -> None:
        for t in history().txns:
            if t.kind is TxnKind.DISCRETIONARY:
                assert t.amount < ZERO


class TestBills:
    def test_a_fixed_bill_lands_on_its_day_every_month_for_the_same_amount(self) -> None:
        rent = [t for t in history().txns if t.label == "rent"]

        assert {t.day.day for t in rent} == {1}
        assert {t.amount for t in rent} == {money("-1800.00")}

    def test_a_variable_bill_varies(self) -> None:
        utilities = {t.amount for t in history().txns if t.label == "utilities"}

        assert len(utilities) > 1

    def test_a_bill_is_never_an_inflow_even_when_the_sample_goes_negative(self) -> None:
        """A wildly variable bill must not become income when its sample dips below zero."""
        h = history(
            bills=(BillSpec(label="wild", day_of_month=5, mean=money("50.00"), sd=money("500.00")),)
        )

        for t in h.txns:
            if t.kind is TxnKind.BILL:
                assert t.amount <= ZERO


class TestShocks:
    """Each is individually toggleable. The base household has none of them."""

    def test_a_missed_paycheck_removes_exactly_that_deposit(self) -> None:
        missed = date(2026, 2, 13)  # a payday under the base spec
        base = history()
        shocked = history(shocks=ShockSpec(missed_paycheck_on=missed))

        assert missed in {t.day for t in base.txns if t.kind is TxnKind.PAYROLL}
        assert missed not in {t.day for t in shocked.txns if t.kind is TxnKind.PAYROLL}

    def test_a_one_off_large_expense_lands_on_its_day(self) -> None:
        when = date(2026, 3, 4)
        h = history(shocks=ShockSpec(large_expense=(when, money("2200.00"))))

        hits = [t for t in h.txns if t.day == when and t.kind is TxnKind.SHOCK]

        assert [t.amount for t in hits] == [money("-2200.00")]

    def test_a_spend_regime_change_raises_the_baseline_from_its_date(self) -> None:
        """A new baby, a medical event — history stops predicting the present."""
        when = START + timedelta(days=90)
        h = history(shocks=ShockSpec(spend_regime_change=(when, Decimal("2.5"))))

        def mean_spend(lo: date, hi: date) -> float:
            xs = [-t.amount for t in h.txns if t.kind is TxnKind.DISCRETIONARY and lo <= t.day < hi]
            return float(statistics.mean(xs))

        before = mean_spend(START, when)
        after = mean_spend(when, START + timedelta(days=DAYS))

        assert after > before * 1.5

    def test_an_early_bill_lands_before_its_usual_day(self) -> None:
        """The landlord cashes the April rent check four days early — so it lands in March.

        This is the shock that makes a forecast wrong without anything looking wrong: the
        money is gone before the projection expected it, and the trough moves.
        """
        h = history(shocks=ShockSpec(early_bill=("rent", date(2026, 4, 1), 4)))

        rent_days = {t.day for t in h.txns if t.label == "rent"}

        assert date(2026, 3, 28) in rent_days
        assert date(2026, 4, 1) not in rent_days
        assert date(2026, 3, 1) in rent_days  # every other month is untouched

    def test_the_base_household_has_no_shocks(self) -> None:
        assert not [t for t in history().txns if t.kind is TxnKind.SHOCK]


class TestCard:
    def test_the_household_pays_its_card_every_month(self) -> None:
        payments = [t for t in history().txns if t.kind is TxnKind.CARD_PAYMENT]

        assert {t.day.day for t in payments} == {20}
        assert {t.amount for t in payments} == {money("-400.00")}


class TestSpecValidation:
    def test_a_history_shorter_than_a_day_is_refused(self) -> None:
        with pytest.raises(ValueError):
            generate(spec(), start=START, days=0, seed=1)

    def test_a_negative_pay_variation_is_refused(self) -> None:
        with pytest.raises(ValueError):
            PayrollSpec(
                net_pay=money("2600.00"),
                cadence=PayCadence.BIWEEKLY,
                first_payday=START,
                variation=Decimal("-0.1"),
            )

    def test_a_zero_day_probability_outside_zero_to_one_is_refused(self) -> None:
        with pytest.raises(ValueError):
            SpendSpec(zero_day_probability=1.5, median=money("38.00"), log_sigma=0.9)


class TestCardCharges:
    """The card is finally a payment instrument, not just a thing that gets paid down.

    Before this, every DISCRETIONARY txn hit checking and `CardSpec.payment` was a monthly
    constant. The card could never be *charged*. Everything downstream — the grader, the spend
    model, the whole calibration story — had therefore never seen a household use a credit card
    the way households use credit cards.
    """

    def test_at_zero_card_share_the_history_is_byte_identical_to_the_old_one(self) -> None:
        """The default must not move a single number already in this repo.

        `card_share=0.0` draws no extra rng — the channel decision is short-circuited — so the
        sequence is exactly the one this generator produced before cards existed. This is what
        lets the simulator learn to charge a card without re-deriving every committed figure on
        the same day.
        """
        history = generate(spec(), START, days=90, seed=7)

        assert not history.card_charges()
        # A hash of the whole txn stream: any drift in amount, day, order or kind trips this.
        fingerprint = [(t.day, t.amount, t.label, t.kind) for t in history.txns]
        expected = [
            (t.day, t.amount, t.label, t.kind)
            for t in generate(spec(), START, days=90, seed=7).txns
        ]
        assert fingerprint == expected
        assert all(t.kind is not TxnKind.CARD_CHARGE for t in history.txns)

    def test_a_charge_does_not_move_the_checking_balance_on_the_day_it_posts(self) -> None:
        """The single most important property in the file.

        A card charge is not a checking outflow. It is a checking outflow *scheduled for the
        due date of the statement it lands on*. Counting it on the posting day too would spend
        the same dollar twice — once when swiped, once when the statement is paid.
        """
        cash_only = generate(spec(), START, days=40, seed=11)
        on_card = generate(
            spec(spend=SpendSpec(0.25, money("38.00"), 0.9, card_share=1.0)),
            START,
            days=40,
            seed=11,
        )

        assert on_card.card_charges(), "the household should have charged something"

        # Every discretionary dollar moved to the card. Checking is now strictly better off on
        # a day before any statement has been paid.
        day = START + timedelta(days=5)
        assert on_card.balance_on(day) > cash_only.balance_on(day)

        # And the charges are real money, just not *yet* checking's problem.
        charged = -sum(t.amount for t in on_card.card_charges())
        assert charged > ZERO

    def test_discretionary_spend_that_moved_to_the_card_leaves_the_checking_series(self) -> None:
        """This is the collapse that makes the whole feature urgent.

        `daily_discretionary_high` is a p90 of *checking* spend. Move that spend onto a card and
        the series falls toward zero — the forecast stops reserving for spend at all, while the
        real obligation reappears a month later as a statement payment. An under-reserve, bought
        by nothing but connecting a real credit card.
        """
        cash_only = generate(spec(), START, days=90, seed=3)
        on_card = generate(
            spec(spend=SpendSpec(0.25, money("38.00"), 0.9, card_share=1.0)),
            START,
            days=90,
            seed=3,
        )

        assert sum(cash_only.discretionary_series()) > ZERO
        assert sum(on_card.discretionary_series()) == ZERO

    def test_a_transactor_pays_the_statement_not_the_constant(self) -> None:
        """A transactor clears what closed. Reserving their *minimum* against that is the hole."""
        transactor = spec(
            spend=SpendSpec(0.0, money("50.00"), 0.0, card_share=1.0),
            cards=(
                CardSpec(
                    balance=ZERO,  # nothing carried; the statement is purely what they charged
                    apr=Decimal("0.1899"),
                    minimum_payment=money("40.00"),
                    payment=money("400.00"),  # their "habitual" amount — a transactor ignores it
                    payment_day_of_month=20,
                    close_day_of_month=20,
                    behavior=PaymentBehavior.TRANSACTOR,
                ),
            ),
        )
        history = generate(transactor, START, days=60, seed=5)

        payments = [t for t in history.txns if t.kind is TxnKind.CARD_PAYMENT]
        assert payments, "a transactor still pays"

        first = payments[0]
        charged_before_close = history.card_charged_between("card-1", START, date(2026, 1, 20))
        # They paid the statement — not $400, not the $40 minimum.
        assert -first.amount == charged_before_close
        assert -first.amount != money("400.00")
        assert -first.amount != money("40.00")

    def test_a_minimum_only_household_pays_the_minimum(self) -> None:
        min_only = spec(
            cards=(
                CardSpec(
                    balance=money("9000.00"),
                    apr=Decimal("0.2399"),
                    minimum_payment=money("180.00"),
                    payment=money("400.00"),
                    payment_day_of_month=20,
                    behavior=PaymentBehavior.MINIMUM_ONLY,
                ),
            ),
        )
        history = generate(min_only, START, days=60, seed=5)
        payments = [t for t in history.txns if t.kind is TxnKind.CARD_PAYMENT]
        assert all(-t.amount == money("180.00") for t in payments)

    def test_a_revolvers_balance_grows_when_charges_outrun_payments(self) -> None:
        """The household the interest model currently cannot describe.

        `total_interest()` has no concept of new charges and `_check_amortizing()` raises if the
        balance grows. A revolver charging more than they pay has a balance that genuinely does.
        For them the sweep is not the answer, and the honest output is no number at all.
        """
        heavy = spec(
            # ~$100/day charged, against a $400/month payment. This card grows.
            spend=SpendSpec(0.0, money("100.00"), 0.0, card_share=1.0),
            cards=(
                CardSpec(
                    balance=money("2000.00"),
                    apr=Decimal("0.2399"),
                    minimum_payment=money("40.00"),
                    payment=money("400.00"),
                    payment_day_of_month=20,
                    behavior=PaymentBehavior.REVOLVER,
                ),
            ),
        )
        history = generate(heavy, START, days=90, seed=9)

        charged = -sum(t.amount for t in history.card_charges())
        paid = -sum(t.amount for t in history.txns if t.kind is TxnKind.CARD_PAYMENT)
        assert charged > paid, "the card grew — this is the household the sweep cannot help"

    def test_two_charges_either_side_of_the_close_land_on_different_statements(self) -> None:
        """The close date is the seam, and it is load-bearing.

        Getting it wrong by one day moves an entire month of spend across the 30-day horizon
        boundary — the difference between reserving for it and never seeing it.
        """
        household = spec(
            spend=SpendSpec(0.0, money("50.00"), 0.0, card_share=1.0),
            cards=(
                CardSpec(
                    balance=ZERO,
                    apr=Decimal("0.1899"),
                    minimum_payment=money("25.00"),
                    payment=money("500.00"),
                    payment_day_of_month=20,
                    close_day_of_month=20,
                    behavior=PaymentBehavior.TRANSACTOR,
                ),
            ),
        )
        history = generate(household, START, days=75, seed=13)

        # Charges on the 19th and the 21st are three weeks apart in the ledger and a *month*
        # apart in when they actually leave checking.
        before = history.card_charged_between("card-1", date(2026, 1, 19), date(2026, 1, 20))
        after = history.card_charged_between("card-1", date(2026, 1, 21), date(2026, 2, 20))
        assert before > ZERO and after > ZERO

        payments = sorted(
            (t for t in history.txns if t.kind is TxnKind.CARD_PAYMENT), key=lambda t: t.day
        )
        jan, feb = payments[0], payments[1]
        assert jan.day == date(2026, 1, 20)
        assert feb.day == date(2026, 2, 20)
        # The 21st's charge is not in January's payment. It waited a month.
        assert -jan.amount < after + before

    def test_a_second_card_gets_its_own_ledger(self) -> None:
        """Without a second card the portfolio reserve and the coverage gate have nothing to
        bite on — and the second card is the one that overdraws you."""
        two = spec(
            spend=SpendSpec(0.0, money("60.00"), 0.0, card_share=1.0),
            cards=(
                CardSpec(
                    balance=money("14000.00"),
                    apr=Decimal("0.2399"),
                    minimum_payment=money("280.00"),
                    payment=money("450.00"),
                    payment_day_of_month=20,
                    card_id="target-24pct",
                    behavior=PaymentBehavior.REVOLVER,
                    charge_weight=0.0,  # they never charge the card they are paying down
                ),
                CardSpec(
                    balance=ZERO,
                    apr=Decimal("0.1899"),
                    minimum_payment=money("40.00"),
                    payment=money("100.00"),
                    payment_day_of_month=20,
                    card_id="daily-driver-18pct",
                    behavior=PaymentBehavior.TRANSACTOR,
                    charge_weight=1.0,  # everything goes here
                ),
            ),
        )
        history = generate(two, START, days=60, seed=17)

        assert not history.card_charges("target-24pct")
        assert history.card_charges("daily-driver-18pct")

        # Both cards are paid, from one checking account, and the transactor's payment is the
        # one nobody is reserving for.
        payers = {t.card_id for t in history.txns if t.kind is TxnKind.CARD_PAYMENT}
        assert payers == {"target-24pct", "daily-driver-18pct"}

    def test_duplicate_card_ids_are_refused_because_ledgers_would_merge(self) -> None:
        card = CardSpec(
            balance=ZERO,
            apr=Decimal("0.1899"),
            minimum_payment=money("25.00"),
            payment=money("100.00"),
        )
        with pytest.raises(ValueError, match="duplicate card_id"):
            spec(cards=(card, card))

    def test_a_card_share_outside_zero_to_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="card_share"):
            SpendSpec(0.25, money("38.00"), 0.9, card_share=1.5)

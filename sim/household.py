"""Deterministic synthetic households.

`generate(spec, start, days, seed)` produces a `History`: every transaction a household made,
and therefore the exact daily balance they actually had. That realized balance is the ground
truth the shadow-mode harness grades decisions against — it is the answer key the engine is
not allowed to see.

## Determinism, and why it is not negotiable

The same `(spec, start, days, seed)` yields a byte-identical `History` forever. Randomness
comes from an explicitly-seeded `random.Random` instance, never the module-level `random.*`
functions, which read shared global state — a history that depends on whatever else ran first
in the process is not ground truth, and a backtest built on it cannot be reproduced or
argued with.

## Point-in-time honesty

`History.as_of(day)` **slices**; it does not regenerate. What the household did on day 10 is
the same fact whether you ask on day 10 or day 90. This is the seam the replay driver builds
its snapshots on, and it is the guard against lookahead bias — the classic way a backtest
reports a tail risk that is flattering and false.

## Spend is zero-inflated and right-skewed, deliberately

Real discretionary spend has many $0 days and occasional $400 ones. It is not Gaussian, and
modelling it as Gaussian would quietly defeat the purpose of the exercise: the engine's
`daily_discretionary_high` is a p90 of *this* distribution, and the whole calibration question
is about its **tail**. So the sampler is a zero-inflated lognormal, and there is a test
asserting the mean sits above the median.
"""

from __future__ import annotations

import calendar
import math
import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum

from engine.models import ZERO, money


def _money(value: float) -> Decimal:
    """The one place a sampled float becomes money.

    `money()` refuses floats on purpose — a binary float cannot represent cents, and
    `Decimal(2.675)` quantizes to 2.67. The sampler produces floats by nature, so the
    conversion happens here, once, at the boundary, via `str`. Every other line in this module
    is already in `Decimal`.
    """
    return money(str(round(value, 2)))


class PayCadence(str, Enum):
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    SEMIMONTHLY = "semimonthly"  # the 15th and the last day of the month
    MONTHLY = "monthly"


class TxnKind(str, Enum):
    PAYROLL = "payroll"
    BILL = "bill"
    DISCRETIONARY = "discretionary"
    CARD_PAYMENT = "card_payment"
    SHOCK = "shock"


@dataclass(frozen=True)
class Txn:
    """One movement of money. Signed: positive is in, negative is out."""

    day: date
    amount: Decimal
    label: str
    kind: TxnKind


@dataclass(frozen=True)
class PayrollSpec:
    net_pay: Decimal
    cadence: PayCadence
    first_payday: date
    # Standard deviation of net pay, as a fraction of it. A salaried W2 is ~0.02 (a little
    # overtime, a benefits change); a shift worker is far higher.
    variation: Decimal = ZERO

    def __post_init__(self) -> None:
        if self.variation < ZERO:
            raise ValueError(f"variation={self.variation} cannot be negative")
        if self.net_pay < ZERO:
            raise ValueError(f"net_pay={self.net_pay} cannot be negative")


@dataclass(frozen=True)
class BillSpec:
    label: str
    day_of_month: int
    mean: Decimal
    sd: Decimal = ZERO

    def __post_init__(self) -> None:
        if not 1 <= self.day_of_month <= 31:
            raise ValueError(f"day_of_month={self.day_of_month} must be 1..31")
        if self.sd < ZERO:
            raise ValueError(f"sd={self.sd} cannot be negative")


@dataclass(frozen=True)
class SpendSpec:
    """Daily discretionary spend: zero-inflated lognormal.

    `median` is the median of a *spending* day. `log_sigma` is the shape — larger means a
    fatter right tail, which is where the entire calibration question lives.
    """

    zero_day_probability: float
    median: Decimal
    log_sigma: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.zero_day_probability <= 1.0:
            raise ValueError(f"zero_day_probability={self.zero_day_probability} must be in [0, 1]")
        if self.log_sigma < 0:
            raise ValueError(f"log_sigma={self.log_sigma} cannot be negative")
        if self.median <= ZERO:
            raise ValueError(f"median={self.median} must be positive")


@dataclass(frozen=True)
class CardSpec:
    balance: Decimal
    apr: Decimal  # a rate, never money() — money() would quantize 0.2399 to 0.24
    minimum_payment: Decimal
    # What the household actually pays each month, above the minimum. This is the
    # counterfactual `engine/interest.py` measures a sweep against (prd.md §5.1).
    payment: Decimal
    payment_day_of_month: int = 20


@dataclass(frozen=True)
class ShockSpec:
    """The ways a household's life stops matching its own history. All off by default."""

    # Job loss, unpaid leave, a payroll switch. The deposit simply does not arrive.
    missed_paycheck_on: date | None = None
    # A car transmission, an ER visit.
    large_expense: tuple[date, Decimal] | None = None
    # (from_day, multiplier) — a new baby, a medical regime. Spend steps up and stays up,
    # which is what makes the household's own history stop predicting its present.
    spend_regime_change: tuple[date, Decimal] | None = None
    # (bill_label, its_usual_day, days_early) — the landlord cashes the check early.
    early_bill: tuple[str, date, int] | None = None


@dataclass(frozen=True)
class HouseholdSpec:
    opening_balance: Decimal
    payroll: PayrollSpec
    bills: tuple[BillSpec, ...]
    spend: SpendSpec
    card: CardSpec
    shocks: ShockSpec = field(default_factory=ShockSpec)


@dataclass(frozen=True)
class History:
    """Everything a household did, and therefore everything that was true.

    The engine never sees this. The grader does.
    """

    spec: HouseholdSpec
    start: date
    days: int
    opening_balance: Decimal
    txns: tuple[Txn, ...]  # sorted by day, then by the order they were generated

    @property
    def end(self) -> date:
        return self.start + timedelta(days=self.days - 1)

    def as_of(self, day: date) -> History:
        """The history as it was knowable on `day`. A slice, never a regeneration.

        The seam that keeps lookahead out of the backtest (#4).
        """
        return History(
            spec=self.spec,
            start=self.start,
            days=(day - self.start).days + 1,
            opening_balance=self.opening_balance,
            txns=tuple(t for t in self.txns if t.day <= day),
        )

    def _by_day(self, kind: TxnKind | None = None) -> dict[date, Decimal]:
        """One pass, not one pass per day.

        The replay driver (#4) walks every day of every household and asks for balances at
        each step. Naive `sum(t for t in txns if ...)` per day is quadratic, and a backtest
        nobody wants to run is a backtest nobody runs.
        """
        totals: dict[date, Decimal] = {}
        for t in self.txns:
            if kind is None or t.kind is kind:
                totals[t.day] = totals.get(t.day, ZERO) + t.amount
        return totals

    def balance_on(self, day: date) -> Decimal:
        """The checking balance at the end of `day`."""
        return self.opening_balance + sum((t.amount for t in self.txns if t.day <= day), ZERO)

    def daily_balances(self, start: date, through: date) -> tuple[Decimal, ...]:
        """The realized balance on each day in `[start, through]`.

        This is the answer key: the grader (#3) compares the engine's *projected* low against
        the minimum of this series.
        """
        moves = self._by_day()
        balance = self.balance_on(start - timedelta(days=1))

        out, day = [], start
        while day <= through:
            balance += moves.get(day, ZERO)
            out.append(balance)
            day += timedelta(days=1)
        return tuple(out)

    def discretionary_on(self, day: date) -> Decimal:
        """Discretionary spend on `day`, as a positive magnitude."""
        return -self._by_day(TxnKind.DISCRETIONARY).get(day, ZERO)

    def discretionary_series(self) -> tuple[Decimal, ...]:
        """Daily discretionary spend across the whole history, positive, zeros included.

        The input to the falsification run: is `30 x p90_daily` — what `forecast.py` assumes
        today — anywhere near the real distribution of 30-day spend?
        """
        spend = self._by_day(TxnKind.DISCRETIONARY)
        return tuple(-spend.get(self.start + timedelta(days=i), ZERO) for i in range(self.days))


def _paydays(payroll: PayrollSpec, start: date, through: date) -> Iterator[date]:
    if payroll.cadence is PayCadence.SEMIMONTHLY:
        yield from _semimonthly(payroll.first_payday, through)
        return

    if payroll.cadence is PayCadence.MONTHLY:
        yield from _monthly(payroll.first_payday, payroll.first_payday.day, through)
        return

    step = timedelta(days=7 if payroll.cadence is PayCadence.WEEKLY else 14)
    day = payroll.first_payday
    while day <= through:
        if day >= start:
            yield day
        day += step


def _semimonthly(first: date, through: date) -> Iterator[date]:
    """The 15th and the last day of each month — the standard US semimonthly calendar."""
    year, month = first.year, first.month
    while True:
        for day_of_month in (15, calendar.monthrange(year, month)[1]):
            day = date(year, month, day_of_month)
            if day > through:
                return
            if day >= first:
                yield day
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def _monthly(first: date, day_of_month: int, through: date) -> Iterator[date]:
    year, month = first.year, first.month
    while True:
        # A bill due on the 31st is due on the 28th in February. Clamp rather than crash.
        last = calendar.monthrange(year, month)[1]
        day = date(year, month, min(day_of_month, last))
        if day > through:
            return
        if day >= first:
            yield day
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def generate(spec: HouseholdSpec, start: date, days: int, seed: int) -> History:
    """Produce one household's realized life. Deterministic in `(spec, start, days, seed)`."""
    if days < 1:
        raise ValueError(f"days={days} must be at least 1")

    rng = random.Random(seed)
    through = start + timedelta(days=days - 1)
    txns: list[Txn] = []
    shocks = spec.shocks

    # --- income ---------------------------------------------------------------------
    sd = float(spec.payroll.net_pay) * float(spec.payroll.variation)
    for day in _paydays(spec.payroll, start, through):
        if day == shocks.missed_paycheck_on:
            # The deposit does not arrive. Nothing marks it — which is exactly the point:
            # the *absence* is the signal, and detecting it is a product feature.
            continue

        amount = float(spec.payroll.net_pay)
        if sd > 0:
            amount = max(0.0, rng.gauss(amount, sd))

        txns.append(Txn(day=day, amount=_money(amount), label="payroll", kind=TxnKind.PAYROLL))

    # --- recurring bills ------------------------------------------------------------
    for bill in spec.bills:
        for day in _monthly(start, bill.day_of_month, through):
            when = day
            if shocks.early_bill and shocks.early_bill[0] == bill.label:
                label, usual, days_early = shocks.early_bill
                if day == usual:
                    when = day - timedelta(days=days_early)

            amount = float(bill.mean)
            if bill.sd > ZERO:
                # Clamped at zero: a wildly variable bill must never sample its way into
                # becoming *income*, which would hand the household money it never had.
                amount = max(0.0, rng.gauss(amount, float(bill.sd)))

            txns.append(Txn(day=when, amount=-_money(amount), label=bill.label, kind=TxnKind.BILL))

    # --- the card payment the household was already making --------------------------
    for day in _monthly(start, spec.card.payment_day_of_month, through):
        txns.append(
            Txn(
                day=day,
                amount=-spec.card.payment,
                label="card payment",
                kind=TxnKind.CARD_PAYMENT,
            )
        )

    # --- daily discretionary spend --------------------------------------------------
    for offset in range(days):
        day = start + timedelta(days=offset)

        if rng.random() < spec.spend.zero_day_probability:
            continue  # a day they spent nothing

        # Lognormal: median x exp(sigma x N(0,1)). Right-skewed by construction — most days
        # are ordinary, a few are a car repair. The tail is the whole point.
        amount = float(spec.spend.median) * math.exp(rng.gauss(0.0, spec.spend.log_sigma))

        if shocks.spend_regime_change:
            from_day, multiplier = shocks.spend_regime_change
            if day >= from_day:
                amount *= float(multiplier)

        txns.append(
            Txn(day=day, amount=-_money(amount), label="discretionary", kind=TxnKind.DISCRETIONARY)
        )

    # --- one-off shock --------------------------------------------------------------
    if shocks.large_expense:
        when, amount = shocks.large_expense
        if start <= when <= through:
            txns.append(Txn(day=when, amount=-amount, label="large expense", kind=TxnKind.SHOCK))

    # Stable order: by day, then by the order generated. `sorted` is stable, so equal days
    # keep their insertion order and the history stays byte-identical across runs.
    txns.sort(key=lambda t: t.day)

    return History(
        spec=spec,
        start=start,
        days=days,
        opening_balance=spec.opening_balance,
        txns=tuple(txns),
    )

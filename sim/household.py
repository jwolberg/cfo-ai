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

from engine.models import ZERO, PaymentBehavior, money


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
    # A charge on a card ledger. **Not a checking outflow** — it is a checking outflow
    # scheduled for the due date of the statement it lands on, and the CARD_PAYMENT txn is
    # where it finally leaves. Anything that sums checking must exclude this kind, or the
    # same dollar is spent twice: once when it is charged, once when the statement is paid.
    CARD_CHARGE = "card_charge"


# Every kind that actually moves the checking balance. CARD_CHARGE is the one that does not,
# and keeping this list explicit is what stops a future kind from being silently double-counted.
CHECKING_KINDS = frozenset(
    {TxnKind.PAYROLL, TxnKind.BILL, TxnKind.DISCRETIONARY, TxnKind.CARD_PAYMENT, TxnKind.SHOCK}
)


@dataclass(frozen=True)
class Txn:
    """One movement of money. Signed: positive is in, negative is out."""

    day: date
    amount: Decimal
    label: str
    kind: TxnKind
    # Which card ledger this belongs to. Set on CARD_CHARGE and CARD_PAYMENT; None otherwise.
    card_id: str | None = None


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
    # The fraction of discretionary spend that goes on a card rather than out of checking.
    #
    # Zero is today's behaviour and the default *deliberately*: at 0.0 the generated history
    # is byte-identical to the one this repo has always produced, so every existing number
    # stays put until a caller opts in. Every household in the real world is somewhere above
    # zero, which is the whole problem this feature exists to fix.
    card_share: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.zero_day_probability <= 1.0:
            raise ValueError(f"zero_day_probability={self.zero_day_probability} must be in [0, 1]")
        if self.log_sigma < 0:
            raise ValueError(f"log_sigma={self.log_sigma} cannot be negative")
        if self.median <= ZERO:
            raise ValueError(f"median={self.median} must be positive")
        if not 0.0 <= self.card_share <= 1.0:
            raise ValueError(f"card_share={self.card_share} must be in [0, 1]")


@dataclass(frozen=True)
class CardSpec:
    """A card the household holds — and, now, actually charges.

    `payment` is no longer what the household pays. It is what a REVOLVER *habitually* pays.
    What actually leaves checking each month is a function of `behavior` and the statement
    that closed — see `_card_payment_for`. That is the change this whole feature turns on:
    once the payment is determined by what was charged, there is no constant left to hardcode,
    and `backend/precompute.py`'s ORDINARY workaround has nothing left to stand on.
    """

    balance: Decimal
    apr: Decimal  # a rate, never money() — money() would quantize 0.2399 to 0.24
    minimum_payment: Decimal
    # What a REVOLVER habitually pays, above the minimum. This is the counterfactual
    # `engine/interest.py` measures a sweep against (prd.md §5.1). A TRANSACTOR ignores it and
    # pays the closed statement; a MINIMUM_ONLY household ignores it and pays the minimum.
    payment: Decimal
    payment_day_of_month: int = 20
    card_id: str = "card-1"
    behavior: PaymentBehavior = PaymentBehavior.REVOLVER
    # When the statement closes. The payment lands `payment_day_of_month` — which in the real
    # world is the due date, a grace period after this.
    close_day_of_month: int = 20
    # The share of *card* spend that lands on this card, when a household holds several.
    # Normalized across cards at generation time.
    charge_weight: float = 1.0

    def __post_init__(self) -> None:
        if self.charge_weight < 0:
            raise ValueError(f"charge_weight={self.charge_weight} cannot be negative")
        if self.balance < ZERO:
            raise ValueError(f"balance={self.balance} cannot be negative")


def _card_payment_for(spec: CardSpec, closed_statement: Decimal) -> Decimal:
    """What actually leaves checking on this card's payment day.

    The minimum is what the *issuer* will accept. This is what the *household* pays, and for
    two of the three behaviours those are very different numbers — which is precisely why the
    reserve may not use the minimum as a proxy for the obligation.
    """
    if spec.behavior is PaymentBehavior.TRANSACTOR:
        # They clear the statement. Reserving their minimum against it is the $1,960 hole.
        return closed_statement
    if spec.behavior is PaymentBehavior.MINIMUM_ONLY:
        return min(spec.minimum_payment, closed_statement)
    # REVOLVER (and UNKNOWN, which the engine blocks on rather than simulating differently):
    # a habitual amount above the minimum, but never more than is owed.
    return min(max(spec.payment, spec.minimum_payment), closed_statement)


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
    # Plural, because a portfolio reserve and a coverage gate need more than one card to have
    # anything to bite on. The second card is the one that overdraws you: you sweep optimally
    # to the 24% card while a transactor card you also hold quietly takes $2,000 on the 20th.
    cards: tuple[CardSpec, ...]
    shocks: ShockSpec = field(default_factory=ShockSpec)

    def __post_init__(self) -> None:
        if not self.cards:
            raise ValueError("a household with no cards has nothing to sweep to")
        ids = [c.card_id for c in self.cards]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate card_id in {ids} — ledgers would silently merge")

    @property
    def card(self) -> CardSpec:
        """The first card. A convenience for single-card callers and tests.

        Deliberately *not* a compatibility shim for the reserve: anything that reserves, ranks
        or forecasts must iterate `cards`, because reading `.card` on a two-card household is
        exactly the bug this feature exists to fix.
        """
        return self.cards[0]


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

        With no `kind`, this is **checking only** — CARD_CHARGE is excluded. A card charge does
        not move the checking balance on the day it posts; it moves it on the day the statement
        it landed on is paid, and the CARD_PAYMENT txn already carries that. Counting both is
        spending the same dollar twice.
        """
        totals: dict[date, Decimal] = {}
        for t in self.txns:
            if kind is None:
                if t.kind not in CHECKING_KINDS:
                    continue
            elif t.kind is not kind:
                continue
            totals[t.day] = totals.get(t.day, ZERO) + t.amount
        return totals

    def balance_on(self, day: date) -> Decimal:
        """The checking balance at the end of `day`.

        Card charges are excluded — see `_by_day`. This is the single most important line in
        the file: a charge is not a checking outflow.
        """
        return self.opening_balance + sum(
            (t.amount for t in self.txns if t.day <= day and t.kind in CHECKING_KINDS), ZERO
        )

    def card_charges(self, card_id: str | None = None) -> tuple[Txn, ...]:
        """Every charge on a card ledger, optionally for one card."""
        return tuple(
            t
            for t in self.txns
            if t.kind is TxnKind.CARD_CHARGE and (card_id is None or t.card_id == card_id)
        )

    def card_charged_between(self, card_id: str, start: date, through: date) -> Decimal:
        """What was charged to `card_id` in `[start, through]`, as a positive magnitude.

        This is what a statement *is*: the charges that landed between one close and the next.
        """
        return -sum(
            (t.amount for t in self.card_charges(card_id) if start <= t.day <= through), ZERO
        )

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


def _last_close_on_or_before(day: date, close_day_of_month: int) -> date:
    """The statement close on or before `day`. Clamped to the month's length."""
    last = calendar.monthrange(day.year, day.month)[1]
    this_month = date(day.year, day.month, min(close_day_of_month, last))
    if this_month <= day:
        return this_month

    year, month = (day.year - 1, 12) if day.month == 1 else (day.year, day.month - 1)
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(close_day_of_month, last))


def _weighted_choice(
    cards: tuple[CardSpec, ...], weights: list[float], total: float, draw: float
) -> CardSpec:
    """Pick a card by weight from a single uniform draw.

    One draw, not `rng.choices` — the sequence of rng calls is the determinism contract here,
    and a helper that draws a variable number of times would make the history depend on how
    many cards a household happens to hold.
    """
    target = draw * total
    running = 0.0
    for card, weight in zip(cards, weights, strict=True):
        running += weight
        if target < running:
            return card
    return cards[-1]


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

    # --- daily discretionary spend, now split by channel -----------------------------
    #
    # Collected here but appended *after* the card payments below, so the assembled txn order
    # matches what this function has always produced. What actually preserves byte-identity at
    # `card_share=0` is that the rng is drawn in the same sequence as before — not the order
    # things are appended.
    spend_txns: list[Txn] = []
    charges: list[Txn] = []

    weights = [c.charge_weight for c in spec.cards]
    total_weight = sum(weights)

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

        # Short-circuited on the left, deliberately: at card_share=0 the rng is never drawn,
        # so the sequence is identical to the one this generator produced before cards existed
        # and every number already in the repo stays put until a caller opts in.
        on_card = spec.spend.card_share > 0.0 and rng.random() < spec.spend.card_share

        if not on_card:
            spend_txns.append(
                Txn(
                    day=day,
                    amount=-_money(amount),
                    label="discretionary",
                    kind=TxnKind.DISCRETIONARY,
                )
            )
            continue

        # Which card. Short-circuited too — a one-card household draws no extra rng.
        card = spec.cards[0]
        if len(spec.cards) > 1 and total_weight > 0:
            card = _weighted_choice(spec.cards, weights, total_weight, rng.random())

        charges.append(
            Txn(
                day=day,
                amount=-_money(amount),
                label="discretionary",
                kind=TxnKind.CARD_CHARGE,
                card_id=card.card_id,
            )
        )

    # --- the card payment, now a consequence rather than a constant ------------------
    #
    # This is the change the whole feature turns on. The payment is no longer a hardcoded
    # number — it is what `behavior` does to the statement that closed. A TRANSACTOR clears it;
    # a MINIMUM_ONLY household pays the floor; a REVOLVER pays their habitual amount. Once the
    # payment is determined by what was charged, there is no constant left to hardcode, and
    # `backend/precompute.py`'s ORDINARY workaround has nothing left to stand on.
    for card in spec.cards:
        outstanding = card.balance
        unbilled = [t for t in charges if t.card_id == card.card_id]

        for pay_day in _monthly(start, card.payment_day_of_month, through):
            close = _last_close_on_or_before(pay_day, card.close_day_of_month)

            # The statement is everything owed as of its close: what was already carried, plus
            # everything charged up to that close.
            billed = -sum((t.amount for t in unbilled if t.day <= close), ZERO)
            statement = max(outstanding + billed, ZERO)
            payment = _card_payment_for(card, statement)

            outstanding = statement - payment
            # Those charges are now part of `outstanding`. Billing them again next cycle would
            # pay for the same coffee twice.
            unbilled = [t for t in unbilled if t.day > close]

            if payment <= ZERO:
                continue

            txns.append(
                Txn(
                    day=pay_day,
                    amount=-payment,
                    label="card payment",
                    kind=TxnKind.CARD_PAYMENT,
                    card_id=card.card_id,
                )
            )

    txns.extend(spend_txns)
    txns.extend(charges)

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

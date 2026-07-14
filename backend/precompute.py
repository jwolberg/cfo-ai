"""Generate the demo household's decision history, day by day.

This is a build-time step, not a request path. It walks one fixed household through one
fixed policy, calls `decide()` once per day, and writes the served window to the JSON
artifact `backend/main.py` loads at startup. Running it twice against the same spec and
seed produces a byte-identical file — a test asserts it.

## What is new here, and why it had to be

Nothing in `engine/` or `sim/` composes a `HouseholdSpec` into a *sequence* of `Snapshot`s.
`sim/` generates the household's realized life; `engine/` decides against one snapshot at a
time. The walk between them — carrying the debt ledger, the rolling week of sweeps, and the
rolling statistics forward from one day to the next — is state that neither side owns today,
and getting any of it wrong is silent rather than loud:

- **Interest accrual.** Without it the card's balance would sit flat across three months
  while the household pays it down, understating the balance and therefore the interest
  the sweeps avoided — the one number the product is graded on (`prd.md` §5.1).
- **`swept_this_week`.** `Snapshot.swept_this_week` defaults to zero, so leaving it unset
  would silently mean `WEEKLY_CAP` could never bind across the entire window — the engine
  would look like it has a weekly cap it never applies.
- **Sweeps against checking.** A sweep is money leaving the funding account. `sim/`'s
  realized history knows nothing about our sweeps, so they are subtracted here; otherwise
  the household's checking balance would be untouched by three months of daily payments
  and the engine would keep finding surplus that, in the world it just created, is gone.

## The one thing this deliberately does not model

The engine's own gates for stale balances, unhealthy connections, and in-flight sweeps are
about a live Plaid connection this demo does not have. Rather than invent failures, the walk
holds those inputs at their healthy values (fresh balance, healthy connection, nothing in
flight) and lets the refusals that *do* appear come from the household's actual cash
position. A sweep settles at the start of the next day — which is also why yesterday's sweep,
not today's, is the one that lands on the ledger.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from backend.artifact import (
    SCHEMA_VERSION,
    Artifact,
    DayRecord,
    SpendSnapshot,
    dump,
    summarize,
)
from engine.decide import decide, untouchable
from engine.models import (
    MIN_GRACE_DAYS,
    ZERO,
    Account,
    AccountKind,
    Action,
    Card,
    CardPortfolio,
    CashEvent,
    ConnectionState,
    CoverageState,
    Decision,
    EventKind,
    PaymentBehavior,
    Snapshot,
    SpendProfile,
    StatementCycle,
    UnmatchedPayment,
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
    _monthly,
    _paydays,
    generate,
)
from sim.household import (
    _last_close_on_or_before as _sim_last_close,
)

DAYS_PER_YEAR = Decimal("365")

# The card's statement closes — and its minimum falls due — on this day of the month.
STATEMENT_DAY = 20

# The engine refuses below 60 days of history (`engine.decide.MIN_HISTORY_DAYS`). Walking a
# warm-up runway before the served window starts means the feed opens on real decisions
# rather than a wall of INSUFFICIENT_HISTORY refusals. The engine is not being fooled — those
# refusals genuinely happen, they are simply not the part of the story worth showing.
WARMUP_DAYS = 60
SERVED_DAYS = 90

# Fixed calendar dates, not "today minus N". Two deploys a week apart must show the same
# household doing the same things, or the demo is not reproducible.
WINDOW_START = date(2026, 1, 1)
SEED = 7

# Trailing windows for the rolling statistics the engine reads.
SPEND_LOOKBACK_DAYS = 90
# The spend profile looks back a year and reads its quantile off overlapping 30-day windows —
# the household's own worst months, not a parametric guess at them.
SPEND_PROFILE_DAYS = 365
SPEND_WINDOW_DAYS = 30
# Income is bucketed into 28-day periods rather than calendar months, and this is not a
# detail. This household is paid biweekly, so a calendar month contains two paychecks —
# except the ~4 times a year it contains three. Bucketing by month would score that
# calendar artifact as a 24% swing in income and trip INCOME_TOO_VARIABLE (the gate is
# 25%), refusing to serve a household whose income is in fact perfectly regular. A 28-day
# bucket is the honest measure of a biweekly earner's variability.
INCOME_BUCKET_DAYS = 28
INCOME_BUCKETS = 3

CHECKING_ID = "chk_demo"
SAVINGS_ID = "sav_demo"
CARD_ID = "card_demo"

# Held flat across the window. The persona (USERS.md) is someone who keeps a cushion parked
# where it earns nothing while paying 24% on a card — which is exactly the advisory the
# engine emits as IDLE_CASH_ELSEWHERE, and worth having in the demo rather than designing
# around.
SAVINGS_BALANCE = money("2400.00")

# Tuned, and the tuning is load-bearing. A weekly cap tight enough to be exhausted mid-week
# turns every remaining day into a BELOW_MIN_SWEEP refusal — and `decide()` drops the cap
# reasons on that path (`engine/decide.py`), so the user is told "what's left is under $1.00"
# when the truth is "you have $2,000 spare and you have hit your weekly limit." That copy is
# misleading, it is an engine-side issue rather than a demo-side one, and it is not this
# plan's to fix — so the policy is set wide enough that the household's actual cash position,
# not an exhausted cap, is what does the refusing. See docs/implementation-notes.md.
#
# `max_sweep` was raised from $400 to meet `max_weekly_sweep` when the sweep cadence went
# weekly. The ceiling on money moved is **unchanged** at $1,600/week; what changed is how many
# ACH debits it takes to get there — one, not four. Leaving the per-sweep cap at $400 would
# have quietly cut the household's throughput by 4x and disguised a cadence change as a
# paydown regression. See docs/learnings/2026-07-14-the-cadence-was-inherited-not-chosen.md.
DEMO_POLICY = UserPolicy(
    buffer_floor=money("800.00"),
    max_sweep=money("1600.00"),
    max_weekly_sweep=money("1600.00"),
    min_days_between_sweeps=7,
)

# Close to `tests/test_outcome.py`'s household() helper, which already matches USERS.md's
# persona: stable biweekly income, one card in the $8-40k / 20-30% APR band, and a habit of
# paying well above the minimum.
DEMO_SPEC = HouseholdSpec(
    # Enough of a cushion to be the persona (someone who over-holds cash against a card they
    # are paying 24% on), but not so much that the engine spends the whole window sweeping
    # the ceiling and never has to refuse for want of money.
    opening_balance=money("3200.00"),
    payroll=PayrollSpec(
        net_pay=money("2600.00"),
        cadence=PayCadence.BIWEEKLY,
        first_payday=date(2026, 1, 2),
        variation=Decimal("0.02"),
    ),
    bills=(
        BillSpec(label="rent", day_of_month=1, mean=money("1800.00")),
        BillSpec(label="utilities", day_of_month=8, mean=money("180.00"), sd=money("35.00")),
    ),
    # The household now puts about a third of its discretionary spend on the card, which is
    # what a real household does and what this feature exists to survive. Turning it on is a
    # decision that changes what the engine decides, so it lands here — with the reserve that
    # can carry it — and not a release earlier.
    #
    # This is also the only way the demo exercises the path that matters: charges accrue
    # unbilled, the reserve covers the statement that has not closed yet, and the forecast's
    # p90-of-checking-spend quietly falls as spend moves off checking. Before the obligation
    # reserve, that fall would have *bought a bigger sweep*.
    #
    # 0.15, not 0.35. The share is the dial that absorbs the demo's shape, and it is tuned
    # *here* rather than by softening the reserve — the trade the cadence work warned would be
    # tempting and arrive at the worst moment. At 0.35 the household's checking spend collapses
    # far enough that the engine finds enough genuine surplus to clear the whole card inside 90
    # days, which is a true story about a different household than the one we serve.
    spend=SpendSpec(
        zero_day_probability=0.25, median=money("38.00"), log_sigma=0.9, card_share=0.15
    ),
    cards=(
        CardSpec(
            # $14,000 sits in the middle of the persona's band (USERS.md) and leaves a real
            # balance at the end of the window rather than a "paid off" banner.
            #
            # Do **not** raise this to absorb the bigger sweeps that card charges produce. At
            # $22,000 and 24% the card accrues ~$440/month against a $450 payment: it amortizes
            # at $10 a month, the payoff horizon runs to decades, and `interest_avoided`
            # balloons to a five-figure number that is arithmetically true and completely
            # unverifiable. That is the household `engine/interest.py` refuses to make a claim
            # about, and it is not the persona. The card_share below is the dial to turn.
            balance=money("14000.00"),
            apr=Decimal("0.2399"),
            minimum_payment=money("280.00"),
            payment=money("450.00"),
            payment_day_of_month=STATEMENT_DAY,
            card_id=CARD_ID,
            close_day_of_month=STATEMENT_DAY,
        ),
    ),
)


# --- the debt ledger ----------------------------------------------------------------


@dataclass
class DebtLedger:
    """The card's balance as it actually moves: interest on, payments off, day by day.

    Mirrors `engine/interest.py`'s model exactly, because the two must agree — that module
    computes what a sweep *saves*, and if this ledger's arithmetic drifted from it the demo
    would claim savings against a balance the engine never believed in.

    Interest accrues daily on the principal at `apr / 365` and **posts** to the principal at
    each statement close. Unposted interest does not itself accrue interest: that is what
    "no intra-cycle compounding" means, and it is the average-daily-balance method revolving
    cards actually use. `outstanding` is principal plus what has accrued but not yet posted,
    so the balance the household owes grows every day — including a day with no payment and
    no sweep — without any of that growth being compounded.
    """

    principal: Decimal
    apr: Decimal
    accrued: Decimal = ZERO  # unrounded until it posts

    @property
    def outstanding(self) -> Decimal:
        return money(self.principal + self.accrued)

    def pay(self, amount: Decimal) -> None:
        """Money off the card: principal first, then any unposted interest it overflows into.

        Principal-first is what `engine/interest.py` does (payments come straight off the
        balance; `accrued` is left alone until it posts), and the two models must not drift.
        It is also the model that pays: a payment against the principal shrinks the accrual
        base *today*, which is the entire reason an early sweep beats a late one.

        The overflow matters for exactly one case, and it is not a rounding detail.
        `outstanding` — the balance the engine sees, and therefore the most it will ever
        sweep when `CLEARS_THE_CARD` binds — is principal *plus* unposted interest. A sweep
        of the full outstanding that only retired principal would strand the accrued
        interest, and the card would converge on a balance of a few cents that it could
        never pay off and the engine would never stop refusing to sweep.

        Never below zero: you cannot overpay a card into credit.
        """
        from_principal = min(self.principal, amount)
        self.principal -= from_principal
        self.accrued = max(self.accrued - (amount - from_principal), ZERO)

    def accrue(self, day: date) -> None:
        """One day of interest. Payments must already have landed — see `interest.py`.

        A dollar paid today does not accrue today, which is the whole reason a sweep on the
        2nd is worth more than the same sweep on the 29th.
        """
        if self.principal > ZERO:
            self.accrued += self.principal * (self.apr / DAYS_PER_YEAR)

        if day.day == STATEMENT_DAY:
            self.principal += money(self.accrued)
            self.accrued = ZERO


# --- derived cash events ------------------------------------------------------------


def derive_cash_events(
    spec: HouseholdSpec, today: date, horizon: int = 30
) -> tuple[CashEvent, ...]:
    """The next 30 days of known inflows and outflows, from the household's own spec.

    A shortcut, and a deliberate one: the recurring-event detector `architecture.md` §3.2
    describes does not exist yet, so rather than block this demo on building one, the events
    are read straight off the spec that generated the household. The engine is not being
    handed anything it could not in principle have learned from the transaction history —
    just handed it earlier, and without the detector's errors.

    The ranges and jitter are not decoration. `forecast.py`'s safety property *is* the
    asymmetry it applies to them — income counted late and small, obligations early and
    large. Collapsing every event to a certain point estimate would leave that asymmetry
    with nothing to bite on and quietly turn the conservative forecast into an exact one.

    **The ORDINARY-at-full-value workaround is gone, and this is where it lived.** It emitted
    the card payment as an ORDINARY outflow at its full $450 and knowingly ate a $280
    double-count, because the alternative — tagging it DEBT_MINIMUM, which the forecast skips —
    left `decide()` reserving only the $280 minimum against a $450 payment. That under-counted
    $170 of real outflow, in the one direction that ends in an overdraft.

    It stood up only because the payment was a hardcoded constant. It no longer is: the payment
    is what `behavior` does to the statement that closed, and `untouchable()` now reserves that
    *actual obligation* rather than the minimum the issuer would settle for. So the payment is
    tagged CARD_PAYMENT, the forecast skips it, the reserve carries it, and the arithmetic is
    exact for the first time instead of deliberately wrong in the safe direction.

    The two halves are one mechanism. Skip the event without reserving the obligation and
    nothing accounts for the payment at all.
    """
    through = today + timedelta(days=horizon)
    events: list[CashEvent] = []

    for day in _paydays(spec.payroll, today, through):
        # Payroll lands on the day it lands; the range covers ordinary overtime/benefits
        # drift, and the one-day jitter covers a bank holiday pushing a deposit out.
        net = spec.payroll.net_pay
        events.append(
            CashEvent(
                label="payroll",
                account_id=CHECKING_ID,
                expected_date=day,
                amount=net,
                amount_low=money(net * Decimal("0.97")),
                amount_high=money(net * Decimal("1.03")),
                date_jitter_days=1,
                confidence=1.0,
            )
        )

    for bill in spec.bills:
        for day in _monthly(today, bill.day_of_month, through):
            mean = bill.mean
            events.append(
                CashEvent(
                    label=bill.label,
                    account_id=CHECKING_ID,
                    expected_date=day,
                    amount=-mean,
                    amount_low=-money(mean * Decimal("0.95")),
                    amount_high=-money(mean * Decimal("1.10")),
                    date_jitter_days=2,
                    confidence=1.0,
                )
            )

    for card in spec.cards:
        for day in _monthly(today, card.payment_day_of_month, through):
            # Tagged CARD_PAYMENT, so `forecast.py` skips it: the reserve is the authoritative
            # source for what this card takes out of checking. Counting it here as well would
            # subtract the same payment twice.
            payment = card.payment
            events.append(
                CashEvent(
                    label="card payment",
                    account_id=CHECKING_ID,
                    expected_date=day,
                    amount=-payment,
                    amount_low=-payment,
                    amount_high=-payment,
                    date_jitter_days=1,
                    confidence=1.0,
                    kind=EventKind.CARD_PAYMENT,
                )
            )

    return tuple(events)


# --- rolling statistics -------------------------------------------------------------


def _p90(values: Sequence[Decimal]) -> Decimal:
    """The 90th percentile — the high end of a day's discretionary spend, deliberately.

    Nearest-rank, on the household's *own* zero-inflated distribution (the zero days are in
    the series and belong there). `forecast.py` charges this against every remaining day of
    the horizon.
    """
    if not values:
        return ZERO
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * 90 // 100))  # ceil(n * 0.9), at least 1
    return ordered[rank - 1]


def daily_discretionary_high(history: History, today: date) -> Decimal:
    """p90 of discretionary spend over the trailing window, from history only."""
    seen = history.as_of(today)
    series = seen.discretionary_series()[-SPEND_LOOKBACK_DAYS:]
    return _p90(series)


def income_variation(history: History, today: date) -> float:
    """Coefficient of variation of income over trailing 28-day buckets.

    Floats are fine here and nowhere else in this file: `Snapshot.income_variation` is a
    ratio, not money. See INCOME_BUCKET_DAYS for why the buckets are 28 days and not months.
    Fewer than two whole buckets of history is not enough to say anything about variability,
    and 0.0 is the honest answer — the engine is refusing on INSUFFICIENT_HISTORY on those
    days anyway.
    """
    seen = history.as_of(today)
    payroll = [t for t in seen.txns if t.kind is TxnKind.PAYROLL]

    buckets: list[float] = []
    for i in range(INCOME_BUCKETS):
        end = today - timedelta(days=INCOME_BUCKET_DAYS * i)
        start = end - timedelta(days=INCOME_BUCKET_DAYS - 1)
        if start < history.start:
            break
        buckets.append(float(sum((t.amount for t in payroll if start <= t.day <= end), ZERO)))

    if len(buckets) < 2:
        return 0.0

    mean = statistics.fmean(buckets)
    if mean <= 0:
        return 0.0

    return statistics.pstdev(buckets) / mean


# --- derivation: raw history -> the card types ---------------------------------------
#
# Pure derivation. Nothing here decides anything; it turns what a household *did* into the
# types `decide()` is allowed to look at. See docs/tickets/0012.

# Below this, we have not seen enough cycles to say what a household does with a card, and
# UNKNOWN is a blocking refusal rather than a guess. Three is the same bar `PaymentBehavior`
# documents and the same one `INSUFFICIENT_HISTORY` already implies.
MIN_CYCLES_TO_CLASSIFY = 3
# A transactor who carries a balance has silently lost their grace period and is accruing at
# the full APR today. Two consecutive cycles, not one — one tolerates a single late payment.
CYCLES_TO_BECOME_REVOLVER = 2
# Going the other way takes the full three. The asymmetry is deliberate: being slow to grant a
# grace period costs a slightly smaller sweep. Being quick to grant one means under-reserving
# a household that owes the whole statement.
CYCLES_TO_BECOME_TRANSACTOR = 3

# A recurring outflow smaller than this is not a card payment — it is a subscription.
MIN_PLAUSIBLE_CARD_PAYMENT = money("25.00")
# ...and it must actually recur. A single card-shaped transfer proves nothing.
MIN_MONTHS_TO_BE_RECURRING = 3

# Merchant strings that mean "this money went to a credit card". In production this is Plaid's
# merchant enrichment; here it is the labels our own simulator emits plus the issuer names a
# real funding account would show.
_CARD_MERCHANT_MARKERS = ("card payment", "card svc", "cardmember", "credit crd", "chase card")


def _cycle_bounds(day: date, close_day: int) -> tuple[date, date]:
    """The statement window `day` falls in: (previous close + 1, this close)."""
    close = _sim_last_close(day, close_day)
    previous = _sim_last_close(close - timedelta(days=1), close_day)
    return previous + timedelta(days=1), close


def classify_behavior(history: History, card: CardSpec, today: date) -> tuple[PaymentBehavior, int]:
    """What this household *does* with this card, and how many cycles we watched to say so.

    Returns UNKNOWN below three observed cycles. We refuse rather than guess, because the guess
    is load-bearing twice over: it sets the reserve *and* it decides whether we may claim to
    have saved them any interest at all.
    """
    seen = history.as_of(today)
    payments = [
        t for t in seen.txns if t.kind is TxnKind.CARD_PAYMENT and t.card_id == card.card_id
    ]

    if len(payments) < MIN_CYCLES_TO_CLASSIFY:
        return PaymentBehavior.UNKNOWN, len(payments)

    # For each observed payment: did it clear the statement that had closed, or leave a balance?
    carried: list[bool] = []
    outstanding = card.balance
    for pay in sorted(payments, key=lambda t: t.day):
        window_start, close = _cycle_bounds(pay.day, card.close_day_of_month)
        billed = seen.card_charged_between(card.card_id, window_start, close)
        statement = max(outstanding + billed, ZERO)
        paid = -pay.amount
        outstanding = max(statement - paid, ZERO)
        carried.append(outstanding > ZERO)

    recent = carried[-CYCLES_TO_BECOME_REVOLVER:]
    if len(recent) == CYCLES_TO_BECOME_REVOLVER and all(recent):
        # They have carried a balance two cycles running. Whatever they used to be, they are
        # paying 24% today, and reserving their minimum against a statement they owe in full
        # would be the smaller of the two mistakes available.
        clean = carried[-CYCLES_TO_BECOME_TRANSACTOR:]
        if len(clean) == CYCLES_TO_BECOME_TRANSACTOR and not any(clean):
            return PaymentBehavior.TRANSACTOR, len(carried)
        paid_only_minimum = all(
            -p.amount <= card.minimum_payment for p in sorted(payments, key=lambda t: t.day)[-3:]
        )
        behavior = PaymentBehavior.MINIMUM_ONLY if paid_only_minimum else PaymentBehavior.REVOLVER
        return behavior, len(carried)

    clean = carried[-CYCLES_TO_BECOME_TRANSACTOR:]
    if len(clean) == CYCLES_TO_BECOME_TRANSACTOR and not any(clean):
        return PaymentBehavior.TRANSACTOR, len(carried)

    return PaymentBehavior.REVOLVER, len(carried)


def observed_monthly_payment(history: History, card: CardSpec, today: date) -> Decimal | None:
    """What the household actually pays this card each cycle — the interest counterfactual.

    Derived from **observed payment events**, never from inferred cycle boundaries. That is not
    a stylistic preference: this figure is payments-divided-by-cycles and it feeds the REVOLVER
    reserve directly, so a cycle inference that invents *more, shorter* cycles would divide the
    same payments across a larger denominator and pull the number **down** — shrinking the very
    reserve the inference was meant to protect. Count the payments. Do not model the calendar.
    """
    seen = history.as_of(today)
    payments = [
        -t.amount for t in seen.txns if t.kind is TxnKind.CARD_PAYMENT and t.card_id == card.card_id
    ]
    if len(payments) < MIN_CYCLES_TO_CLASSIFY:
        # No claim rather than a flattering one. `INSUFFICIENT_HISTORY` is already refusing to
        # sweep on these days anyway.
        return None
    return money(sum(payments, ZERO) / len(payments))


def observed_monthly_charges(history: History, card: CardSpec, today: date) -> Decimal | None:
    """What the household puts on this card each cycle.

    Without this the interest model projects a balance that can only ever *shrink* — so a
    household charging more than they pay gets a payoff date that never arrives and an
    interest-avoided figure overstated by construction. That is the number prd.md §5.1 says the
    company is graded on.

    Derived from observed charges over observed cycles. `None` below three cycles, which means
    the model assumes **zero** future charges — the old, flattering behaviour, and reachable
    only while `CARD_BEHAVIOR_UNKNOWN` is already blocking the sweep outright.
    """
    seen = history.as_of(today)
    charges = seen.card_charges(card.card_id)
    if not charges:
        return ZERO

    cycles = len({(t.day.year, t.day.month) for t in seen.txns if t.kind is TxnKind.CARD_PAYMENT})
    if cycles < MIN_CYCLES_TO_CLASSIFY:
        return None

    charged = -sum((t.amount for t in charges), ZERO)
    return money(charged / cycles)


def infer_close_day(history: History, card: CardSpec, today: date) -> tuple[int, bool]:
    """The statement close day, and whether we are *sure* of it.

    Plaid does not reliably return the close date (prd.md §6.2, the same gap that makes APR
    unreliable). Where it is missing we infer it from the household's own payment dates — but
    the inference is only ever allowed to move the obligation *earlier*, into the horizon, never
    later out of it. Getting a close date wrong by one day moves an entire month of spend across
    the horizon boundary; wrong-and-early costs a smaller sweep, wrong-and-late is an overdraft.

    Returns `(day_of_month, certain)`. `certain=False` puts the caller on notice to reserve
    early — see `derive_card`.
    """
    seen = history.as_of(today)
    pay_days = sorted(
        t.day.day for t in seen.txns if t.kind is TxnKind.CARD_PAYMENT and t.card_id == card.card_id
    )
    if len(pay_days) < MIN_CYCLES_TO_CLASSIFY:
        return card.close_day_of_month, False

    # Payments cluster on the due date. A household that pays on the same day every month tells
    # us the cycle exactly; one that pays whenever they remember does not.
    common = max(set(pay_days), key=pay_days.count)
    certain = pay_days.count(common) >= len(pay_days) - 1
    return common, certain


def derive_card(history: History, card: CardSpec, today: date, ledger_balance: Decimal) -> Card:
    """The `Card` the engine sees, as of `today`.

    Splits what is owed into the statement that has **already closed** (a known fact, legally
    due) and the charges since (**unbilled** — not yet due, but the thing that determines next
    month's bill). `obligation_in_horizon()` in U4 needs both, and a Card carrying only the
    closed statement is what left the reserve at $0 for a third of every cycle.
    """
    seen = history.as_of(today)
    behavior, _cycles = classify_behavior(seen, card, today)
    close_day, certain = infer_close_day(seen, card, today)

    cycle = StatementCycle(close_day_of_month=close_day, grace_days=MIN_GRACE_DAYS)
    last_close = _sim_last_close(today, close_day)
    next_close = cycle.close_on_or_after(today + timedelta(days=1))

    # What has posted since the last close is not yet billed.
    unbilled = seen.card_charged_between(card.card_id, last_close + timedelta(days=1), today)
    statement_balance = max(ledger_balance - unbilled, ZERO)

    due = cycle.due_for(last_close)
    if not certain:
        # We are guessing at the calendar. Guess in the direction that reserves: pull the
        # obligation to the near edge of the horizon rather than letting it drift past it.
        due = min(due, today)

    return Card(
        card_id=card.card_id,
        apr=card.apr,
        cycle=cycle,
        statement_balance=statement_balance,
        statement_due_date=max(due, today),
        minimum_payment=card.minimum_payment,
        unbilled_balance=unbilled,
        next_close_date=next_close,
        behavior=behavior,
        observed_monthly_payment=observed_monthly_payment(seen, card, today),
        observed_monthly_charges=observed_monthly_charges(seen, card, today),
    )


def detect_unmatched_payments(
    history: History, known_card_ids: set[str], today: date
) -> tuple[UnmatchedPayment, ...]:
    """Recurring, card-shaped outflows that map to no card we can see.

    The real coverage gate. Attestation is necessary and nowhere near sufficient — people forget
    the store card — but a recurring $300 to `CHASE CARD SVC` with no Chase card connected is
    *evidence*, not a hunch. Deterministic, runs off data we already have, and fails toward
    refusal.
    """
    seen = history.as_of(today)

    candidates: dict[str, list[Txn]] = {}
    for txn in seen.txns:
        if txn.amount >= ZERO:
            continue
        if txn.card_id in known_card_ids:
            continue  # a card we can see. Not our problem.
        label = txn.label.lower()
        if not any(marker in label for marker in _CARD_MERCHANT_MARKERS):
            continue
        if -txn.amount < MIN_PLAUSIBLE_CARD_PAYMENT:
            continue  # a subscription, not a card
        candidates.setdefault(txn.label, []).append(txn)

    out: list[UnmatchedPayment] = []
    for merchant, txns in sorted(candidates.items()):
        months = {(t.day.year, t.day.month) for t in txns}
        if len(months) < MIN_MONTHS_TO_BE_RECURRING:
            continue  # it happened once. That is not a liability, it is a transfer.
        amounts = [-t.amount for t in txns]
        out.append(
            UnmatchedPayment(
                merchant=merchant,
                typical_amount=money(sum(amounts, ZERO) / len(amounts)),
                day_of_month=max({t.day.day for t in txns}, key=[t.day.day for t in txns].count),
                months_observed=len(months),
            )
        )

    return tuple(out)


def derive_portfolio(
    history: History,
    cards: tuple[Card, ...],
    today: date,
    attested: bool,
) -> CardPortfolio:
    """Every card the household is liable for — or an honest statement that we do not know."""
    unmatched = detect_unmatched_payments(history, {c.card_id for c in cards}, today)

    if unmatched:
        coverage = CoverageState.UNMATCHED_PAYMENT
    elif not attested:
        coverage = CoverageState.UNATTESTED
    else:
        coverage = CoverageState.COMPLETE

    return CardPortfolio(cards=cards, coverage=coverage, unmatched_card_payments=unmatched)


def _rolling_30d(daily: Sequence[Decimal]) -> tuple[Decimal, ...]:
    """Every overlapping 30-day total in the series."""
    if len(daily) < SPEND_WINDOW_DAYS:
        return ()
    window = sum(daily[:SPEND_WINDOW_DAYS], ZERO)
    out = [window]
    for i in range(SPEND_WINDOW_DAYS, len(daily)):
        window += daily[i] - daily[i - SPEND_WINDOW_DAYS]
        out.append(window)
    return tuple(out)


def derive_spend_profile(history: History, today: date) -> SpendProfile:
    """What normal looks like, over the trailing year, across every channel.

    Non-parametric by construction. The 2026-07-13 learning is explicit about why: real spend is
    zero-inflated and right-skewed, sigma is a poor description of its tail, and a parametric
    `mu + z*sigma*sqrt(t)` reintroduces exactly the error the current forecast makes — "variance
    grows with sqrt(t), and this model grows it with t". So we enumerate the household's own
    overlapping 30-day windows and read the quantile straight off them.

    **This feeds no decision.** It is a structure and a dashboard. Swapping the forecast onto its
    empirical quantile would *loosen* the reserve, and loosening needs the measured breach rate
    that `engine/outcome.py` cannot yet produce. See U8.
    """
    seen = history.as_of(today)

    cash = list(seen.discretionary_series())
    charges_by_day: dict[date, Decimal] = {}
    for txn in seen.card_charges():
        charges_by_day[txn.day] = charges_by_day.get(txn.day, ZERO) - txn.amount
    card = [charges_by_day.get(seen.start + timedelta(days=i), ZERO) for i in range(seen.days)]

    return SpendProfile(
        window_days=SPEND_PROFILE_DAYS,
        commitments=(),
        by_category={},
        rolling_30d_cash=_rolling_30d(cash),
        rolling_30d_card=_rolling_30d(card),
    )


def assemble_snapshot(
    history: History,
    today: date,
    spec: HouseholdSpec,
    policy: UserPolicy,
    ledger_balance: Decimal,
    checking: Decimal,
    swept_this_week: Decimal = ZERO,
    days_since_last_sweep: int | None = None,
) -> Snapshot:
    """The `Snapshot` the engine sees on `today`, given the walk's state.

    Exported for `backend/replay.py`, and defined **here** on purpose. The replay driver has to
    grade the engine that shipped, not a second reconstruction of it: two copies of "how a
    Snapshot is assembled" would drift, and the day they drifted the calibration numbers would
    quietly start describing an engine that never existed. That is the same argument
    `untouchable()` and `apply_caps()` are exported under, and it has already paid for itself once.

    The sweep state is **passed in**, not inferred. A replay that assumed we had never swept would
    never trip `CADENCE_HOLD` — and would therefore never observe a deferral, which is precisely
    the thing the calibration has to partition on. If the engine had been running, it would have
    swept, and the cadence would have held.
    """
    cards = tuple(
        derive_card(history, card_spec, today, ledger_balance=ledger_balance)
        for card_spec in spec.cards
    )

    return Snapshot(
        today=today,
        accounts=(
            Account(
                account_id=CHECKING_ID,
                balance=money(checking),
                connection=ConnectionState.HEALTHY,
                balance_age_days=0,
                kind=AccountKind.CHECKING,
            ),
            Account(
                account_id=SAVINGS_ID,
                balance=SAVINGS_BALANCE,
                connection=ConnectionState.HEALTHY,
                balance_age_days=0,
                kind=AccountKind.SAVINGS,
            ),
        ),
        funding_account_id=CHECKING_ID,
        events=derive_cash_events(spec, today),
        pending=(),
        portfolio=derive_portfolio(history, cards, today, attested=True),
        policy=policy,
        daily_discretionary_high=daily_discretionary_high(history, today),
        income_variation=income_variation(history, today),
        history_days=(today - history.start).days + 1,
        sweeps_in_flight=ZERO,
        swept_this_week=swept_this_week,
        days_since_last_sweep=days_since_last_sweep,
    )


# --- the walk -----------------------------------------------------------------------


def _next_due(on: date, day_of_month: int) -> date:
    """The first statement due date on or after `on`."""
    for day in _monthly(on, day_of_month, on + timedelta(days=40)):
        return day
    raise AssertionError("a monthly date always falls within 40 days")  # pragma: no cover


def build(
    spec: HouseholdSpec = DEMO_SPEC,
    start: date = WINDOW_START,
    warmup_days: int = WARMUP_DAYS,
    served_days: int = SERVED_DAYS,
    seed: int = SEED,
    policy: UserPolicy = DEMO_POLICY,
) -> Artifact:
    """Walk the household day by day and return the served window as an artifact."""
    total_days = warmup_days + served_days
    history = generate(spec, start=start, days=total_days, seed=seed)

    ledger = DebtLedger(principal=spec.card.balance, apr=spec.card.apr)
    card_payments = {t.day: -t.amount for t in history.txns if t.kind is TxnKind.CARD_PAYMENT}

    sweeps: dict[date, Decimal] = {}  # the day a sweep was *decided*
    swept_cumulative = ZERO  # settled sweeps, already out of checking
    served: list[DayRecord] = []

    for offset in range(total_days):
        today = start + timedelta(days=offset)
        yesterday = today - timedelta(days=1)

        # Yesterday's sweep settles at the start of today: off the card, and out of the
        # checking account that `sim/` — which knows nothing of our sweeps — still shows.
        if settled := sweeps.get(yesterday, ZERO):
            ledger.pay(settled)
            swept_cumulative += settled

        # The household's own card payment, straight from the realized history.
        if payment := card_payments.get(today, ZERO):
            ledger.pay(payment)

        # Payments land, then the day's interest accrues on what is left.
        ledger.accrue(today)

        checking = history.balance_on(today) - swept_cumulative

        # Sweeps already made in the trailing week, which is what `WEEKLY_CAP` measures its
        # headroom against. Six prior days plus today makes the seven.
        swept_this_week = sum(
            (
                amount
                for day, amount in sweeps.items()
                if today - timedelta(days=6) <= day <= yesterday
            ),
            ZERO,
        )

        # Spacing since the last sweep we actually decided. None until the first one, which is
        # what makes a brand-new household eligible on day one rather than serving it a week of
        # holds it did nothing to earn.
        last_sweep = max(sweeps, default=None)
        days_since_last_sweep = (today - last_sweep).days if last_sweep else None

        # The card as the engine sees it: the statement already closed, and the charges since
        # that will become next month's. `derive_card` reads both, because a reserve keyed only
        # on the closed statement falls to $0 for a third of every cycle.
        #
        # The demo household attests to its card list — it has exactly one card and we generated
        # it. Coverage is COMPLETE, not because attestation is a formality, but because there is
        # genuinely nothing here we cannot see.
        # Every card, always — including one that has been paid to zero. A paid-off card is
        # still a card we can *see*, and dropping it from the portfolio was a real bug: the
        # coverage detector then read the household's own historical payments to it as evidence
        # of a card we could not see, and refused with CARD_COVERAGE_INCOMPLETE on the very days
        # the feed should have been celebrating a cleared balance.
        #
        # `_select_target` already filters on `total_owed > 0` and answers NO_DEBT when nothing
        # is open. Emptiness is its job to decide, not this loop's.
        cards = tuple(
            derive_card(history, card_spec, today, ledger_balance=ledger.outstanding)
            for card_spec in spec.cards
        )
        portfolio = derive_portfolio(history, cards, today, attested=True)

        snapshot = Snapshot(
            today=today,
            accounts=(
                Account(
                    account_id=CHECKING_ID,
                    balance=money(checking),
                    connection=ConnectionState.HEALTHY,
                    balance_age_days=0,
                    kind=AccountKind.CHECKING,
                ),
                Account(
                    account_id=SAVINGS_ID,
                    balance=SAVINGS_BALANCE,
                    connection=ConnectionState.HEALTHY,
                    balance_age_days=0,
                    kind=AccountKind.SAVINGS,
                ),
            ),
            funding_account_id=CHECKING_ID,
            events=derive_cash_events(spec, today),
            pending=(),
            portfolio=portfolio,
            policy=policy,
            daily_discretionary_high=daily_discretionary_high(history, today),
            income_variation=income_variation(history, today),
            history_days=(today - history.start).days + 1,
            sweeps_in_flight=ZERO,
            swept_this_week=swept_this_week,
            days_since_last_sweep=days_since_last_sweep,
        )

        decision: Decision = decide(snapshot)

        if decision.action is Action.SWEEP:
            sweeps[today] = decision.amount

        if offset >= warmup_days:
            served.append(
                DayRecord(
                    day=today,
                    decision=decision,
                    checking_balance=money(checking),
                    savings_balance=SAVINGS_BALANCE,
                    buffer_floor=policy.buffer_floor,
                    debt_balance=ledger.outstanding,
                    debt_apr=spec.card.apr,
                    debt_id=CARD_ID,
                    history_days=snapshot.history_days,
                )
            )
            # The last served day's view of the card is the one the Spending screen renders.
            final = (today, portfolio, untouchable(snapshot)[1])

    days = tuple(served)
    _assert_demo_is_worth_showing(days)

    return Artifact(
        version=SCHEMA_VERSION,
        window_start=days[0].day,
        window_end=days[-1].day,
        days=days,
        summary=summarize(days),
        spend=derive_spend_snapshot(history, *final),
    )


def derive_spend_snapshot(
    history: History, today: date, portfolio: CardPortfolio, reserved: Decimal
) -> SpendSnapshot:
    """What the Spending screen renders. Comprehension, not a decision.

    Every figure here is *reported*. None of it feeds `forecast.py` — swapping the forecast onto
    `worst_30d_cash` would **loosen** the reserve, and loosening needs the measured breach rate
    `engine/outcome.py` cannot yet produce (U8). The panel ships a release *before* it is trusted
    with a decision, deliberately: it earns its way into the forecast having already been looked
    at by real households.
    """
    profile = derive_spend_profile(history, today)
    card = portfolio.cards[0] if portfolio.cards else None

    if card is None:
        return SpendSnapshot(
            statement_balance=ZERO,
            statement_due=today,
            unbilled_balance=ZERO,
            unbilled_due=today,
            reserved=reserved,
            rolling_30d_cash=profile.rolling_30d_cash,
            rolling_30d_card=profile.rolling_30d_card,
            charged_last_cycle=ZERO,
            paid_last_cycle=ZERO,
        )

    # The cycle just gone — what they put on the card against what they took off it. If the
    # first number is bigger, the card grew, and a sweep is not what fixes that.
    window_start, close = _cycle_bounds(today, card.cycle.close_day_of_month)
    seen = history.as_of(today)
    charged = seen.card_charged_between(card.card_id, window_start, close)
    paid = -sum(
        (
            t.amount
            for t in seen.txns
            if t.kind is TxnKind.CARD_PAYMENT
            and t.card_id == card.card_id
            and window_start <= t.day <= close
        ),
        ZERO,
    )

    return SpendSnapshot(
        statement_balance=card.statement_balance,
        statement_due=card.statement_due_date,
        unbilled_balance=card.unbilled_balance,
        unbilled_due=card.cycle.due_for(card.next_close_date),
        reserved=reserved,
        rolling_30d_cash=profile.rolling_30d_cash,
        rolling_30d_card=profile.rolling_30d_card,
        charged_last_cycle=charged,
        paid_last_cycle=paid,
    )


def _assert_demo_is_worth_showing(days: tuple[DayRecord, ...]) -> None:
    """Fail the build rather than ship a one-sided demo.

    A window that is all sweeps says the engine is a spender; a window that is all refusals
    says it is broken. The product's actual claim is that it does both, for stated reasons,
    and an artifact that cannot show that is not worth deploying. This is a build-time
    assertion precisely so it is caught here and not in front of an interviewer.
    """
    if not days:
        raise ValueError("served window is empty")

    actions = {r.decision.action for r in days}
    if Action.SWEEP not in actions:
        raise ValueError(
            f"served window of {len(days)} days contains no SWEEP — "
            "the spec, seed, or policy needs tuning"
        )
    if Action.REFUSE not in actions:
        raise ValueError(
            f"served window of {len(days)} days contains no REFUSE — "
            "the spec, seed, or policy needs tuning"
        )


def main(path: Path | None = None) -> None:
    from backend.artifact import DEFAULT_PATH

    artifact = build()
    dump(artifact, path or DEFAULT_PATH)

    summary = artifact.summary
    print(f"wrote {path or DEFAULT_PATH}")
    print(f"  window:          {artifact.window_start} .. {artifact.window_end}")
    print(f"  days:            {len(artifact.days)}")
    print(f"  sweeps/refusals: {summary.sweep_count} / {summary.refuse_count}")
    print(f"  swept:           ${summary.total_swept}")
    print(f"  interest avoided:${summary.interest_avoided_total}")
    print(f"  card balance:    ${summary.targeted_debt_balance}")


if __name__ == "__main__":
    main()

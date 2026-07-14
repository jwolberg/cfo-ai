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

from backend.artifact import SCHEMA_VERSION, Artifact, DayRecord, dump, summarize
from engine.decide import decide
from engine.models import (
    ZERO,
    Account,
    AccountKind,
    Action,
    CashEvent,
    ConnectionState,
    Debt,
    Decision,
    Snapshot,
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
    TxnKind,
    _monthly,
    _paydays,
    generate,
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
    # card_share stays at its default of 0.0 in this unit. The demo household still charges
    # nothing, so this artifact is byte-for-byte the one already committed — the simulator can
    # now issue charges, but turning them on for the demo is a decision that changes what the
    # engine decides, and it belongs with the reserve that can survive it (U4), not here.
    spend=SpendSpec(zero_day_probability=0.25, median=money("38.00"), log_sigma=0.9),
    cards=(
        CardSpec(
            # A $9,000 card — the low end of the persona's band — is one this household clears
            # almost exactly inside a 90-day window, which lands the demo on a $0 balance and a
            # "paid off" banner instead of the engine's actual daily work. $14,000 sits in the
            # middle of the band (USERS.md) and still has a real balance at the end of the
            # window, which is the story worth showing.
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

    The card payment is emitted as an ORDINARY outflow at its full amount, not as
    DEBT_MINIMUM. The household pays $450 against a $280 minimum (`DEMO_SPEC`); tagging the
    whole payment as the minimum would make `forecast.py` skip all $450 of it (see `EventKind`)
    while `decide()` reserved only the $280 — under-counting $170 of real outflow, in the one
    direction that ends in an overdraft. Reserving the minimum on top of the full payment
    over-counts by $280 instead, which is the direction that costs a slightly smaller sweep.

    Figures are `DEMO_SPEC`'s, and they have drifted once already: this paragraph narrated
    $400/$180/$220 long after the spec moved to $450/$280, and a scoping document later copied
    the stale numbers back out of it. If `DEMO_SPEC` changes, change these too.
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

    for day in _monthly(today, spec.card.payment_day_of_month, through):
        payment = spec.card.payment
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

        debt = Debt(
            debt_id=CARD_ID,
            balance=ledger.outstanding,
            minimum_payment=spec.card.minimum_payment,
            minimum_due_date=_next_due(today, STATEMENT_DAY),
            apr=spec.card.apr,
            # What they were paying before we arrived — the counterfactual the interest
            # claim is measured against, and without which the engine makes no claim at all.
            observed_monthly_payment=spec.card.payment,
        )

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
            debts=(debt,) if ledger.outstanding > ZERO else (),
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

    days = tuple(served)
    _assert_demo_is_worth_showing(days)

    return Artifact(
        version=SCHEMA_VERSION,
        window_start=days[0].day,
        window_end=days[-1].day,
        days=days,
        summary=summarize(days),
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

"""Conservative cash-flow projection.

One rule governs everything here:

    Money arrives late and small. Money leaves early and large.

Every uncertain quantity is resolved toward the end of its range that hurts the user.
This is not pessimism for its own sake — it is the only asymmetry that makes a
probabilistic forecast safe to bet someone's rent on. A forecast that is right on
average will overdraft half its users half the time.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from engine.models import ZERO, Account, EventKind, Snapshot

HORIZON_DAYS = 30

# An inflow we are less sure of than this is not counted as money at all.
INCOME_CONFIDENCE_FLOOR = 0.80


def funding_account(snapshot: Snapshot) -> Account | None:
    for a in snapshot.accounts:
        if a.account_id == snapshot.funding_account_id:
            return a
    return None


def _spend_per_day(snapshot: Snapshot) -> Decimal:
    """What we charge against each remaining day of the horizon.

    ## The old model, and why it over-reserves

    `daily_discretionary_high` is a p90 of *daily* spend, and the forecast charged it against
    every one of the 30 days — an effective reserve of `30 x p90_daily`. That is wrong, and it is
    wrong by about 5.5x of the padding: **variance grows with sqrt(t), and that model grows it
    with t.** A 30-day sum has 30x the mean but only ~sqrt(30) ~ 5.5x the standard deviation.

    Measured over three years of daily spend for three household shapes, it reserved **more than
    the household's worst 30-day stretch had ever been** — not a conservative estimate of a bad
    month, a month worse than any they have ever had. The over-reserve was **$400-970** against a
    $750 default buffer, so on a large share of days this single error was the entire difference
    between sweeping and refusing (`docs/learnings/2026-07-13-the-spend-model-over-reserves.md`).

    ## The new model

    `spend_30d_high` is the household's own worst plausible 30-day total, read straight off their
    **own** enumerated rolling windows. Non-parametric by construction: no distributional
    assumption, their real skew and autocorrelation, and explainable in one sentence — *"your
    worst 30-day stretch last year was $2,231."* Deterministic, because the "sampling" is their
    own history, which is already in the `Snapshot`.

    It is amortized flat across the horizon rather than front-loaded. The **total** is what the
    reserve is about; the daily shape only decides which day the projected low lands on, and a
    flat charge makes no claim about *when* in the month they spend that we cannot support.

    `None` falls back to the old model. That fallback is not a nicety — it is what lets this ship
    inert and be turned on against a measured breach rate rather than an argument.
    """
    if snapshot.spend_30d_high is None:
        return snapshot.daily_discretionary_high
    return snapshot.spend_30d_high / HORIZON_DAYS


def _project_horizon(snapshot: Snapshot) -> tuple[tuple[date, Decimal], ...]:
    """One walk of the funding account across the horizon — the shared source of both the low and
    the trajectory, so they can never disagree about the same forecast (ticket 0064).

    Returns the balance points the low is taken over, in order. The **first** point is `(today,
    current balance)` — where the household is *now*, before today's own expected events; every
    point after it is `(day, balance at the end of that day)` for `day` in `today..today+HORIZON`.
    That "now" anchor is a real candidate for the low: a household whose balance only ever climbs is
    at its lowest today, and dropping it would let the chart's minimum disagree with the decision's.
    """
    account = funding_account(snapshot)
    if account is None:
        raise ValueError(f"funding account {snapshot.funding_account_id!r} not in snapshot")

    balance = account.balance

    # Pending debits against this account have not posted, but the money is already
    # spoken for. Pending *credits* are ignored: we do not spend money the bank has not
    # handed over.
    for p in snapshot.pending:
        if p.account_id == account.account_id and p.amount < ZERO:
            balance += p.amount

    # A sweep we initiated but that has not settled may not be reflected in the
    # balance yet. Subtract it, or we will spend the same dollar twice.
    balance -= snapshot.sweeps_in_flight

    daily: dict[date, Decimal] = {}

    for event in snapshot.events:
        if event.account_id != account.account_id:
            # Income paid into savings does not protect a checking-account debit.
            continue

        if event.kind is EventKind.CARD_PAYMENT:
            # Already reserved from available cash by decide(), out of Debt — the
            # authoritative source. Subtracting it here too would double-count it.
            continue

        if event.is_inflow:
            if event.confidence < INCOME_CONFIDENCE_FLOOR:
                continue  # not money — a hope
            # Late and small.
            when = event.expected_date + timedelta(days=event.date_jitter_days)
            amount = min(event.amount, event.amount_low)
        else:
            # Early and large. Outflows are counted regardless of confidence: an
            # obligation we are unsure of is still an obligation we might owe.
            when = event.expected_date - timedelta(days=event.date_jitter_days)
            if when < snapshot.today:
                when = snapshot.today
            amount = min(event.amount, event.amount_high)  # more negative = larger

        daily[when] = daily.get(when, ZERO) + amount

    # The "now" anchor, then each day's end balance. `today` therefore appears twice — the current
    # balance and the balance after today's own events — which are equal on the common day (no event
    # dated exactly today) and differ, honestly, when a charge is pulled forward to today.
    points: list[tuple[date, Decimal]] = [(snapshot.today, balance)]
    for offset in range(HORIZON_DAYS + 1):
        day = snapshot.today + timedelta(days=offset)
        balance += daily.get(day, ZERO)
        if offset > 0:
            balance -= _spend_per_day(snapshot)
        points.append((day, balance))

    return tuple(points)


def balance_trajectory(snapshot: Snapshot) -> tuple[tuple[date, Decimal], ...]:
    """The funding account's projected balance over the horizon, for the chart (ticket 0064).

    The `(day, balance)` curve `conservative_low_balance` finds its low on — the *same walk*, so the
    chart's lowest point and the decision's low are always the same number. See `_project_horizon`
    for why `today` is the first point and may appear twice.
    """
    return _project_horizon(snapshot)


def conservative_low_balance(snapshot: Snapshot) -> tuple[Decimal, date]:
    """Return (low_balance, day_it_occurs) for the *funding account* over the horizon.

    Only the funding account is projected, and this is the whole point. An ACH debit
    leaves one specific account. A user with $100 in checking and $5,000 in savings has
    $5,100 of money and $100 of *protection* — summing them and testing the total
    against the buffer would authorize a sweep that overdraws checking while the savings
    sits there untouched. Savings is real money, but it is not *there*, and moving it is
    a second ACH with its own delay and its own failure modes.

    The low balance — not the ending balance — is what constrains a sweep. The user only
    has to be broke once. The low is the minimum of `_project_horizon`'s walk, earliest day
    winning ties (the walk's first point is `today`, so a household that only gains is `today`).
    """
    points = _project_horizon(snapshot)

    low_day, low = points[0]
    for day, balance in points[1:]:
        if balance < low:
            low = balance
            low_day = day

    return low, low_day

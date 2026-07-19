"""The linked-history adapter — a real Plaid-linked household becomes a `sim.History` the engine can
decide against. Ticket 0056 capstone; fills `backend/livepath.py:linked_history`.

This is the last seam of the link→decision loop. Everything upstream is built and merged: Plaid
ingest lands transactions (`plaid_transactions`) and balances + card terms (`plaid_accounts`,
`plaid_liabilities`, migration 0014); `backend/recurring.py` finds the paychecks and bills; and
`backend/livepath.py` re-decides a household from its current policy + attestation. What was missing
is the piece here — turning those rows into the `History` (a `HouseholdSpec` + typed `Txn`s) that
`walk()`/`assemble_snapshot` read.

**Only the decision-relevant spec is inferred, on purpose.** `assemble_snapshot` reads the payroll
and `spec.bills` (through `derive_cash_events` — the *future* schedule the forecast leans on) and
`spec.cards` (through `derive_card`); the snapshot's spend statistics come from the *history*
(`daily_discretionary_high`, `spend_30d_high`), not `spec.spend`, and the balances are passed in. So
`spec.spend` is a harmless default and the work is in payroll, bills, cards, and the txn stream.

**It produces a decision to *display*, not to move money.** A linked household today is a Plaid
**Sandbox** one, and the payment rail is in **shadow** — so a first-cut approximation is a wrong
*number on a screen*, never a wrong transfer. That is what makes shipping v1 honest. The two
approximations are named where they live:

1. **The card ledger is anchored at the current balance** (`CardSpec.balance` = today's Plaid
   balance), not reconstructed from a full charge/payment cycle. The walk then applies the real card
   payments and daily accrual, so the today-balance drifts from the current by at most a window of
   accrual — a display error, and one the exact-cycle *normalize* step (`architecture.md` §3.2) will
   later remove.
2. **Unbilled card charges are not reconstructed** (no `CARD_CHARGE` txns) — the reserve covers the
   closed statement Plaid reports, and the not-yet-billed spend is the same deferred normalize step.

When the data cannot support a decision — too little history, no depository account, no card, no
detectable income — it raises `livepath.NoLinkedHistory` rather than inventing one. Failing to a
refusal is the safe direction.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from backend.db.repository import Repository
from backend.livepath import NoLinkedHistory
from backend.precompute import _CARD_MERCHANT_MARKERS, ESTIMATED_APR, money
from backend.recurring import Movement, RecurringStream, detect_recurring, normalize_name
from engine.models import UserPolicy  # noqa: F401  (re-exported convenience for callers)
from sim.household import (
    ZERO,
    BillSpec,
    CardSpec,
    History,
    HouseholdSpec,
    PayCadence,
    PayrollSpec,
    SpendSpec,
    Txn,
    TxnKind,
)

# The engine refuses to decide on less than this much history (`precompute`/`decide`); below it the
# household is not assemble-able into a decision, so we say so rather than serve a refusal that
# looks like a data problem.
_MIN_HISTORY_DAYS = 60

_CADENCES: dict[str, PayCadence] = {
    "weekly": PayCadence.WEEKLY,
    "biweekly": PayCadence.BIWEEKLY,
    "semimonthly": PayCadence.SEMIMONTHLY,
    "monthly": PayCadence.MONTHLY,
}

# `spec.spend` is not read by the decision path (see the module docstring), so a linked household
# takes this placeholder rather than an inferred distribution — the spend statistics that *do* reach
# the snapshot come from the transaction history, not from here.
_PLACEHOLDER_SPEND = SpendSpec(zero_day_probability=0.3, median=money("30.00"), log_sigma=0.9)


def build_linked_history(repo: Repository) -> History:
    """A linked household's `History`, assembled from its ingested Plaid data. Raises
    `NoLinkedHistory` when the data cannot support a decision (the safe direction)."""
    accounts = repo.latest_plaid_accounts()
    checking = _checking_account(accounts)
    if checking is None:
        raise NoLinkedHistory(f"{repo.household_id}: no depository account to fund from")

    card_accounts = [a for a in accounts if _is_credit(a)]
    if not card_accounts:
        raise NoLinkedHistory(f"{repo.household_id}: no card to decide about")

    checking_id = checking["plaid_account_id"]
    rows = [
        r
        for r in repo.plaid_transactions()
        if r["plaid_account_id"] == checking_id
        and r["amount"] is not None
        and r["date"] is not None
    ]
    if not rows:
        raise NoLinkedHistory(f"{repo.household_id}: no checking transactions ingested")

    # Natural accounting sign (positive in, negative out) — Plaid's convention is the opposite, so
    # every amount flips on the way in (the discipline `backend/recurring.py` documents).
    movements = [
        Movement(day=r["date"], amount=-Decimal(r["amount"]), name=_label(r)) for r in rows
    ]
    start, today = min(m.day for m in movements), max(m.day for m in movements)
    days = (today - start).days + 1
    if days < _MIN_HISTORY_DAYS:
        raise NoLinkedHistory(
            f"{repo.household_id}: {days}d of history, engine needs {_MIN_HISTORY_DAYS}d"
        )

    streams = detect_recurring(movements)
    payroll = _payroll(streams, movements)
    if payroll is None:
        raise NoLinkedHistory(f"{repo.household_id}: no recurring income detected")

    liabilities = {liab["plaid_account_id"]: liab for liab in repo.latest_plaid_liabilities()}
    cards = tuple(_card_spec(a, liabilities.get(a["plaid_account_id"])) for a in card_accounts)
    bills = _bills(streams)

    # The card a card-shaped payment pays down. v1 holds one card; with several, the payment goes to
    # the first — merchant→card attribution is the normalize step, and no money moves on it here.
    payoff_card_id = cards[0].card_id
    bill_keys = {b.label for b in bills}
    txns = tuple(
        sorted(
            (_txn(m, bill_keys, payoff_card_id) for m in movements), key=lambda t: (t.day, t.label)
        )
    )

    # Opening balance chosen so the walk's checking balance lands on the *current* Plaid balance at
    # `today`: balance_on(today) = opening + Σ(checking txns), so opening = current − Σ.
    current_checking = _dec(checking.get("current_balance"))
    checking_delta = sum((t.amount for t in txns if t.kind in _CHECKING_KINDS), ZERO)
    opening = money(current_checking - checking_delta)

    spec = HouseholdSpec(
        opening_balance=opening,
        payroll=payroll,
        bills=bills,
        spend=_PLACEHOLDER_SPEND,
        cards=cards,
    )
    return History(spec=spec, start=start, days=days, opening_balance=opening, txns=txns)


_CHECKING_KINDS = frozenset(
    {TxnKind.PAYROLL, TxnKind.BILL, TxnKind.DISCRETIONARY, TxnKind.CARD_PAYMENT}
)


def _label(row: dict[str, Any]) -> str:
    return row.get("name") or row.get("merchant_name") or ""


def _dec(value: Any) -> Decimal:
    return ZERO if value is None else Decimal(value)


def _is_credit(account: dict[str, Any]) -> bool:
    return (account.get("type") or "").lower() == "credit"


def _checking_account(accounts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The funding account: a `checking` subtype if present, else any depository. v1 takes the first
    match; several checking accounts is the multi-account case a later rung scopes."""
    depository = [a for a in accounts if (a.get("type") or "").lower() == "depository"]
    checking = [a for a in depository if (a.get("subtype") or "").lower() == "checking"]
    picked = checking or depository
    return picked[0] if picked else None


def _is_card_payment(name: str) -> bool:
    label = name.lower()
    return any(marker in label for marker in _CARD_MERCHANT_MARKERS)


def _txn(movement: Movement, bill_keys: set[str], payoff_card_id: str) -> Txn:
    """Classify one checking movement into a typed `Txn`. Inflows are income; outflows are a card
    payment (a card-shaped merchant), a bill (a detected recurring outflow), or discretionary."""
    if movement.amount > ZERO:
        kind, card_id = TxnKind.PAYROLL, None
    elif _is_card_payment(movement.name):
        kind, card_id = TxnKind.CARD_PAYMENT, payoff_card_id
    elif normalize_name(movement.name) in bill_keys:
        kind, card_id = TxnKind.BILL, None
    else:
        kind, card_id = TxnKind.DISCRETIONARY, None
    return Txn(
        day=movement.day, amount=movement.amount, label=movement.name, kind=kind, card_id=card_id
    )


def _payroll(streams: list[RecurringStream], movements: list[Movement]) -> PayrollSpec | None:
    """The household's income, from the highest-confidence recurring **inflow** whose cadence the
    engine models. `first_payday` is the earliest observed occurrence of that stream — a real day on
    its schedule, which is what `_paydays` projects forward from."""
    for stream in streams:  # already sorted most-confident first
        cadence = _CADENCES.get(stream.cadence)
        if stream.direction != "inflow" or cadence is None:
            continue
        anchor = min(
            (m.day for m in movements if m.amount > ZERO and normalize_name(m.name) == stream.name),
            default=None,
        )
        if anchor is None:  # pragma: no cover — the stream came from these movements
            continue
        return PayrollSpec(net_pay=stream.typical_amount, cadence=cadence, first_payday=anchor)
    return None


def _bills(streams: list[RecurringStream]) -> tuple[BillSpec, ...]:
    """The recurring **outflows** that are bills — not card payments (those are a checking `Txn` the
    reserve already accounts for, not a scheduled bill to forecast twice)."""
    return tuple(
        BillSpec(
            label=stream.name[:40] or "bill",
            day_of_month=stream.day_of_month,
            mean=stream.typical_amount,
        )
        for stream in streams
        if stream.direction == "outflow"
        and stream.cadence in ("monthly", "semimonthly")
        and not _is_card_payment(stream.name)
    )


def _card_spec(account: dict[str, Any], liability: dict[str, Any] | None) -> CardSpec:
    """A `CardSpec` from a Plaid credit account and its liability terms. The balance is anchored at
    the current Plaid balance (approximation #1); the APR is the reported purchase rate, or the
    estimated `0.23` with `apr_reported=False` so the engine flags it as a guess (ticket 0028)."""
    balance = abs(_dec(account.get("current_balance")))
    liability = liability or {}
    apr = liability.get("purchase_apr")
    minimum = liability.get("minimum_payment")
    minimum_payment = (
        _dec(minimum)
        if minimum is not None
        else max(money("25.00"), money(balance * Decimal("0.02")))
    )
    return CardSpec(
        balance=money(balance),
        apr=_dec(apr) if apr is not None else ESTIMATED_APR,
        apr_reported=apr is not None,
        minimum_payment=minimum_payment,
        # v1: a revolver pays about the minimum. What actually leaves checking is the real
        # CARD_PAYMENT txns in the history; the counterfactual `interest.py` measures against.
        payment=minimum_payment,
        payment_day_of_month=_day_of(liability.get("next_payment_due_date"), 20),
        close_day_of_month=_day_of(liability.get("last_statement_issue_date"), 20),
        card_id=account["plaid_account_id"],
    )


def _day_of(value: date | None, default: int) -> int:
    """A calendar day-of-month from a date, clamped to 1..28 (the engine's `close_day` bound)."""
    if value is None:
        return default
    return min(max(value.day, 1), 28)

"""The served window, rebuilt from rows. Ticket 0024.

`backend/main.py` served one household from one committed JSON file. This is where that stops:
the same `Artifact`/`DayRecord`/`Summary` shapes come back, assembled from the `decisions` and
`snapshots` rows the seeder wrote, scoped to one household by `0021`'s repository and again by RLS.

## The shapes survive; only the source moves

Nothing about the wire format changes here. `Artifact` is still the response, `summarize()` still
computes the headline stats, and `engine/explain.py` still writes every sentence. That is
deliberate — a migration of the *source* is hard enough to verify without also being a rewrite, and
archetype A's `/decisions` response is the oracle: identical, day for day, to what the file served.

## Why the display fields come from the snapshot

`decisions` carries what a metric needs — action, amount, target, reasons, the projection — and
`architecture.md` [3.3] stores the whole frozen `Snapshot` beside it for explanation, audit and
replay. The dashboard needs neither exactly: it wants checking, savings, the buffer, and the
portfolio, which live in the snapshot.

So the feed reads both, in **two queries** — the decisions for a window, then every snapshot they
point at, through `0022`'s seam. Not ninety `get()` calls: the store is a network away and that is
most of a second to draw one screen. And not a denormalized display subset on the `decisions` row,
which would duplicate what the snapshot already holds and let the two disagree about what the
engine saw.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from backend.artifact import DayRecord, DebtRecord, Summary, summarize
from backend.codec import decode_scalar, decode_tree
from backend.db.repository import Repository
from backend.db.snapshots import SnapshotStore
from backend.spend import SpendProjection, SpendSurface, assemble
from engine.models import (
    AccountKind,
    Action,
    ConnectionState,
    Decision,
    EventKind,
    Reason,
    ReasonCode,
    Snapshot,
)

_ENUMS = {
    cls.__name__: cls for cls in (Action, ReasonCode, AccountKind, ConnectionState, EventKind)
}


class NoSuchHousehold(LookupError):
    """The household does not exist. A 404, not a 500 — nothing is wrong with the service."""


class NoSpendProjection(LookupError):
    """The household has decisions but no spend projection. Ticket `0031`.

    Distinct from `NoSuchHousehold` because it is a **different fault**: the household is real and
    its feed renders, and what is missing is the seeded half of the spend surface. Collapsing the
    two would report a seeding bug as "no such household" and send whoever debugs it looking for a
    row that is right there.
    """


class Household:
    """A household the demo can be switched to. Not a customer — see `USERS.md`."""

    def __init__(self, household_id: str, archetype: str | None) -> None:
        self.id = household_id
        self.archetype = archetype

    @property
    def label(self) -> str:
        """A human-readable name for a switcher.

        Derived from the archetype rather than stored: a label is copy, and copy that lives in a
        database is copy nobody can grep for. `null` archetype means a real household (the column's
        own comment), and a real household is not something the demo gets to name.
        """
        return _LABELS.get(self.archetype or "", self.archetype or self.id)


# The archetypes, in words, for a reader picking between them. `backend/archetypes.py` explains
# what each one is *for*; this only has to make them tellable apart in a dropdown.
_LABELS = {
    "demo_biweekly": "Biweekly, one card",
    "semimonthly_portfolio": "Semimonthly, three cards",
    "monthly_thin": "Monthly, two cards",
    "apr_unreported": "Biweekly, two cards, rates unknown",
}


def list_households(conn: Connection, only: list[str] | None = None) -> list[Household]:
    """The households in `only` (a caller's memberships), for the switcher — or every household.

    Not RLS-scoped: `households` is the tenant registry, the one table you read *before* you have a
    household to scope to. Isolation is applied here instead by `only`, the caller's membership set
    resolved in `GET /households` (identity rung, KTD-2) — the route never passes an id the session
    did not earn. `only=None` keeps the unfiltered listing for internal callers (the seeder, the
    nightly poll) that enumerate every household by design. An **empty** `only` returns nothing — a
    user with no memberships sees no households, which is the correct answer, not "all of them".

    It returns ids and labels and nothing else — no balances, no decisions, nothing that an id alone
    should not buy you. Everything past this point goes through the repository and RLS.
    """
    if only is None:
        rows = conn.execute(text("SELECT id, archetype FROM households ORDER BY id")).all()
    elif not only:
        return []
    else:
        rows = conn.execute(
            text("SELECT id, archetype FROM households WHERE id = ANY(:ids) ORDER BY id"),
            {"ids": only},
        ).all()
    return [Household(household_id=r.id, archetype=r.archetype) for r in rows]


@dataclass(frozen=True)
class ServedWindow:
    """One household's served days, and the stats rolled up from them.

    **Not an `Artifact`.** `DayRecord` and `Summary` survive the move to Postgres unchanged — they
    are the response, and the client should not be able to tell where they came from. `Artifact`
    does not: it carries a `version`, because a file has a schema that can drift out from under a
    reader, and nothing versions a query.

    It also carried a `spend` surface this path had no source for, which is what `0031` fixed —
    from the other end. The surface is now `spend.SpendSurface`, served per card from rows, and
    `Artifact` does not have one at all (schema 5). The two types stayed separate and the field
    went away, rather than this one growing a second field that could only be a lie.
    """

    window_start: date
    window_end: date
    days: tuple[DayRecord, ...]
    summary: Summary

    def by_day(self, day: date) -> DayRecord | None:
        """The record for one day, or `None`. Mirrors `Artifact.by_day` — see
        `assistant.DecisionHistory`, which is the interface both of them exist to satisfy."""
        for record in self.days:
            if record.day == day:
                return record
        return None


def load_window(repo: Repository, store: SnapshotStore) -> ServedWindow:
    """One household's served window, rebuilt from rows.

    Raises `NoSuchHousehold` when there is nothing on record — an empty 200 would say the household
    exists and did nothing, which is a different story and a false one.
    """
    rows = repo.decisions()
    if not rows:
        raise NoSuchHousehold(repo.household_id)

    snapshots = store.get_many([r["snapshot_ref"] for r in rows if r["snapshot_ref"]])
    days = tuple(_day_record(row, snapshots[row["snapshot_ref"]]) for row in rows)

    return ServedWindow(
        window_start=days[0].day,
        window_end=days[-1].day,
        days=days,
        summary=summarize(days),
    )


def _day_record(row: dict[str, Any], snapshot: Snapshot) -> DayRecord:
    return DayRecord(
        day=row["day"],
        decision=_decision(row),
        checking_balance=_account_balance(snapshot, AccountKind.CHECKING),
        savings_balance=_account_balance(snapshot, AccountKind.SAVINGS),
        buffer_floor=snapshot.policy.buffer_floor,
        debts=tuple(
            DebtRecord(
                debt_id=card.card_id,
                # What the card actually owes: the closed statement plus what has been charged
                # since. `statement_balance` alone is the closed half, and a dashboard showing it
                # as "your balance" would understate the debt by a month of spending.
                balance=card.total_owed,
                apr=card.apr,
                apr_source=card.apr_source,
            )
            for card in snapshot.portfolio.cards
        ),
        history_days=snapshot.history_days,
    )


def _decision(row: dict[str, Any]) -> Decision:
    low = row["projected_low_balance"]
    return Decision(
        action=Action(row["action"]),
        amount=row["amount"],
        target_debt_id=row["target_card_id"],
        reasons=tuple(
            Reason(
                code=ReasonCode(r["code"]),
                params={k: decode_scalar(v, _ENUMS) for k, v in r["params"].items()},
            )
            for r in row["reasons"]
        ),
        # None on a blocking refusal: it never ran a forecast. The column is nullable for that
        # reason, and a zero here would read as a projection that was exactly right.
        projected_low_balance=None if low is None else Decimal(low),
    )


def _account_balance(snapshot: Snapshot, kind: AccountKind) -> Decimal:
    for account in snapshot.accounts:
        if account.kind is kind:
            return account.balance
    return Decimal("0.00")


def decision_on(repo: Repository, store: SnapshotStore, day: date) -> DayRecord | None:
    """One day. `None` when we have nothing on record for it — the caller renders the 404."""
    row = repo.decision_on(day)
    if row is None:
        return None

    ref = row["snapshot_ref"]
    if not ref:
        return None

    return _day_record(row, store.get(ref))


def load_spend_surface(repo: Repository, store: SnapshotStore) -> SpendSurface:
    """One household's spend surface, per card. Ticket `0031`.

    **Two rows, not ninety.** The surface renders a single day — the last one on record — so this
    reads that one decision, that one snapshot, and the projection. `load_window` pulls the whole
    feed because the feed *is* the window; this would be pulling 90 days to use one.

    The obligations come off the snapshot **live**, through `backend/spend.py`, which is the half of
    `0031`'s first problem that needed no ingest and no projection: the statement, the unbilled
    balance and each card's share of the reserve are all facts about the `Snapshot` the engine
    already saw, and Postgres has been holding it since `0022`.

    Raises `NoSuchHousehold` when there are no decisions, and `NoSpendProjection` when there are
    decisions but nothing derived from the `History` — a household seeded before `0031`, or by
    something that skipped it. Both are refusals rather than an empty surface: a Spending screen
    showing zeros is a household that spends nothing, which is a different story and a false one.
    """
    row = repo.last_decision()
    if row is None:
        raise NoSuchHousehold(repo.household_id)

    ref = row["snapshot_ref"]
    if not ref:
        raise NoSuchHousehold(repo.household_id)

    stored = repo.spend_projection()
    if stored is None:
        raise NoSpendProjection(repo.household_id)

    projection = decode_tree(SpendProjection, stored["payload"])

    return assemble(store.get(ref), projection)

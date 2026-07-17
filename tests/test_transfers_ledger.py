"""The `transfers` ledger through the repository (ticket 0039, the sweep-execution rung).

`tests/test_schema.py::TestTransfers` proves the append-only grant and the CHECKs at the raw-SQL
layer; `tests/test_idor.py` proves isolation with a real row in the leak fixture. These are the
`Repository.add_transfer` / `transfers()` claims — that a write lands under the scoped household and
that the append-only ledger reads back in order — the shape `add_plaid_transaction` is tested at.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import text

from backend.db.repository import repository
from tests.conftest import requires_db

pytestmark = requires_db

ALICE, BOB = "alice", "bob"


@pytest.fixture
def two_households(db):
    """Alice and Bob, each a bare household — arranged as superuser so setup is not under test."""
    with db.begin():
        for h in (ALICE, BOB):
            db.execute(text("INSERT INTO households (id, archetype) VALUES (:h, 'test')"), {"h": h})
    return db


def _add(repo, **overrides):
    """Append a transfer through the repository with sensible defaults."""
    fields = dict(
        transfer_id="xfer-1",
        target_card_id="card-alice",
        decision_id="dec-1",
        decision_date=dt.date(2026, 3, 2),
        leg="debit",
        state="submitted",
        direction="debit",
        amount=Decimal("50.00"),
        provider="increase",
        idempotency_key="alice-2026-03-02-debit-submit",
    )
    fields.update(overrides)
    repo.add_transfer(**fields)


def test_a_transfer_lands_under_the_scoped_household(two_households, app_engine) -> None:
    """The write goes through RLS `WITH CHECK`, so the row binds to the repository's household."""
    with repository(app_engine, ALICE) as repo:
        _add(repo, transfer_id="xfer-a")
    with two_households.begin():
        got = two_households.execute(
            text("SELECT household_id FROM transfers WHERE id='xfer-a'")
        ).scalar()
    assert got == ALICE


def test_the_ledger_reads_back_scoped_and_in_order(two_households, app_engine) -> None:
    """`transfers()` returns this household's rows, oldest first — the append-only trail."""
    with repository(app_engine, ALICE) as repo:
        _add(repo, transfer_id="x1", state="authorized")
        _add(repo, transfer_id="x2", state="submitted", provider_transfer_id="inc-1")
    with repository(app_engine, ALICE) as repo:
        rows = repo.transfers()
    assert [r["id"] for r in rows] == ["x1", "x2"]
    assert [r["state"] for r in rows] == ["authorized", "submitted"]
    assert rows[0]["amount"] == Decimal("50.00")


def test_a_transfer_carries_the_exact_decimal_amount(two_households, app_engine) -> None:
    """NUMERIC through the repository, never a float (ADR-0002 [2.2])."""
    with repository(app_engine, ALICE) as repo:
        _add(repo, transfer_id="x1", amount=Decimal("1234.56"))
    with repository(app_engine, ALICE) as repo:
        got = repo.transfers()[0]["amount"]
    assert got == Decimal("1234.56")
    assert isinstance(got, Decimal)


def test_one_household_cannot_read_anothers_transfers(two_households, app_engine) -> None:
    """The ledger is scoped: bob's repository never sees alice's transfer."""
    with repository(app_engine, ALICE) as repo:
        _add(repo, transfer_id="alice-only", target_card_id="card-alice")
    with repository(app_engine, BOB) as repo:
        assert repo.transfers() == []

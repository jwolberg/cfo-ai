"""The linked-history adapter (`backend/linkedpath.py`) — the capstone of the link→decision loop.

Seeds a linked household's ingested Plaid data (balances, card terms, and a few months of checking
transactions) into a real Postgres, then asserts the whole chain runs: `linked_history()` builds a
`History` from those rows, and `live_decision()` turns it into a real decision. This is the thing
the 0056 seam raised `NoLinkedHistory` for until now.

The Plaid network is not involved — the rows are the ingested output the sync/ingest paths produce,
so this proves the adapter and the assembly against the real engine, not a fake of it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.attestation import card_fingerprint
from backend.db.repository import repository
from backend.livepath import NoLinkedHistory, linked_history, live_decision
from engine.models import Action, AprSource
from sim.household import PayCadence
from tests.conftest import requires_db

pytestmark = requires_db

HH = "hh_linked"
CHK = "acc_chk"
CARD = "acc_card"
ITEM = "item_linked"
START = dt.date(2026, 1, 1)
DAYS = 120  # > the engine's 60-day floor


def _accounts(repo, *, checking_balance: str) -> None:
    repo.add_plaid_account(
        plaid_item_id=ITEM,
        plaid_account_id=CHK,
        name="Checking",
        official_name="Big Bank Checking",
        type="depository",
        subtype="checking",
        current_balance=Decimal(checking_balance),
        available_balance=Decimal(checking_balance),
        iso_currency_code="USD",
    )
    repo.add_plaid_account(
        plaid_item_id=ITEM,
        plaid_account_id=CARD,
        name="Rewards Card",
        official_name="Big Bank Rewards",
        type="credit",
        subtype="credit card",
        current_balance=Decimal("14000.00"),  # amount owed
        available_balance=None,
        iso_currency_code="USD",
    )
    repo.add_plaid_liability(
        plaid_item_id=ITEM,
        plaid_account_id=CARD,
        last_statement_balance=Decimal("1200.00"),
        last_statement_issue_date=dt.date(2026, 4, 18),
        minimum_payment=Decimal("280.00"),
        next_payment_due_date=dt.date(2026, 5, 12),
        purchase_apr=Decimal("0.2399"),
        is_overdue=False,
    )


def _txn(repo, *, day: dt.date, plaid_amount: str, name: str) -> None:
    """One ingested transaction. `plaid_amount` is Plaid's sign — **positive is money out** of the
    account (a purchase/bill), negative is money in (a paycheck)."""
    repo.add_plaid_transaction(
        transaction_id=f"pt_{uuid.uuid4().hex}",
        plaid_item_id=ITEM,
        plaid_account_id=CHK,
        plaid_transaction_id=f"tx_{uuid.uuid4().hex}",
        change_type="added",
        amount=Decimal(plaid_amount),
        date=day,
        name=name,
    )


def _seed_transactions(repo) -> None:
    """A realistic checking history: a biweekly paycheck in, monthly rent + a monthly card payment
    out, and a little discretionary spend — the shapes the detector and the classifier read."""
    # Biweekly payroll (money IN → negative in Plaid's sign).
    day = dt.date(2026, 1, 2)
    while day <= START + dt.timedelta(days=DAYS):
        _txn(repo, day=day, plaid_amount="-2600.00", name="ACME PAYROLL")
        day += dt.timedelta(days=14)
    # Monthly rent, and a monthly card payment (a card-shaped merchant), and some coffee.
    for month in (1, 2, 3, 4):
        _txn(repo, day=dt.date(2026, month, 1), plaid_amount="1800.00", name="TENANT RENT")
        _txn(repo, day=dt.date(2026, month, 15), plaid_amount="300.00", name="CHASE CARD PAYMENT")
        _txn(repo, day=dt.date(2026, month, 9), plaid_amount="42.00", name="BLUE BOTTLE")
        _txn(repo, day=dt.date(2026, month, 22), plaid_amount="63.00", name="CORNER MARKET")


@pytest.fixture
def linked(db, app_engine: Engine):
    with db.begin():
        # A real (linked) household — archetype NULL, not the demo plane.
        db.execute(
            text("INSERT INTO households (id, archetype, is_demo) VALUES (:h, NULL, false)"),
            {"h": HH},
        )
    with repository(app_engine, HH) as repo:
        repo.set_policy(
            buffer_floor=Decimal("500.00"),
            max_sweep=Decimal("1600.00"),
            max_weekly_sweep=Decimal("3200.00"),
            min_days_between_sweeps=7,
            blackout_dates=[],
        )
        _accounts(repo, checking_balance="3200.00")
        _seed_transactions(repo)
        # Attest the household so coverage can clear (a linked household has no engine `cards` rows,
        # so this attests the current — empty — set; it clears UNATTESTED, and the card-payment txn
        # maps to a known card so nothing is an UNMATCHED_PAYMENT).
        repo.add_attestation(card_fingerprint=card_fingerprint(c["id"] for c in repo.cards()))
    return HH


class TestTheLinkedLoop:
    def test_a_linked_household_assembles_a_history_from_its_plaid_data(
        self, linked, app_engine: Engine
    ) -> None:
        with repository(app_engine, HH) as repo:
            history = linked_history(repo)

        # The checking balance is reconstructed to the *current* Plaid balance at the last day.
        assert history.balance_on(history.end) == Decimal("3200.00")
        # Income was detected and typed; the card carries its real, reported APR.
        assert history.spec.payroll.cadence is PayCadence.BIWEEKLY
        assert history.spec.payroll.net_pay == Decimal("2600.00")
        assert len(history.spec.cards) == 1
        assert history.spec.cards[0].apr == Decimal("0.2399")
        assert history.spec.cards[0].apr_reported is True
        # Rent became a scheduled bill; the card payment did not (the reserve accounts for it).
        assert any(b.label.startswith("tenant") for b in history.spec.bills)

    def test_live_decision_runs_end_to_end_on_linked_data(self, linked, app_engine: Engine) -> None:
        with repository(app_engine, HH) as repo:
            history = linked_history(repo)
            decided = live_decision(repo, history, history.end)

        # A real decision, from real ingested data — a sweep or an honest refusal, never a crash.
        assert decided.decision.action in (Action.SWEEP, Action.REFUSE)
        assert decided.decision.reasons  # it explains itself
        # The card the engine saw carries the reported rate, not the 0.23 estimate.
        card = decided.snapshot.portfolio.cards[0]
        assert card.apr == Decimal("0.2399")
        assert card.apr_source is AprSource.REPORTED


class TestItRefusesRatherThanInventing:
    def test_no_card_raises_no_linked_history(self, db, app_engine: Engine) -> None:
        with db.begin():
            db.execute(
                text("INSERT INTO households (id, archetype, is_demo) VALUES (:h, NULL, false)"),
                {"h": HH},
            )
        with repository(app_engine, HH) as repo:
            repo.add_plaid_account(
                plaid_item_id=ITEM,
                plaid_account_id=CHK,
                name="Checking",
                official_name=None,
                type="depository",
                subtype="checking",
                current_balance=Decimal("3200.00"),
                available_balance=Decimal("3200.00"),
                iso_currency_code="USD",
            )
            _seed_transactions(repo)
            with pytest.raises(NoLinkedHistory):
                linked_history(repo)

    def test_too_little_history_raises(self, db, app_engine: Engine) -> None:
        with db.begin():
            db.execute(
                text("INSERT INTO households (id, archetype, is_demo) VALUES (:h, NULL, false)"),
                {"h": HH},
            )
        with repository(app_engine, HH) as repo:
            _accounts(repo, checking_balance="3200.00")
            # Only two weeks of data — well under the 60-day floor.
            for i in range(3):
                _txn(
                    repo,
                    day=START + dt.timedelta(days=i * 5),
                    plaid_amount="-2600.00",
                    name="ACME PAYROLL",
                )
            with pytest.raises(NoLinkedHistory):
                linked_history(repo)

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

from backend.attestation import attested_for, card_fingerprint, current_card_ids
from backend.db.repository import repository
from backend.livepath import NoLinkedHistory, linked_history, live_decision
from engine.models import Action, AprSource, CoverageState, PaymentBehavior
from sim.household import PayCadence, TxnKind
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


def _card_acct_txn(repo, *, day: dt.date, plaid_amount: str, name: str) -> None:
    """One transaction ingested on the **card** account (`CARD`). On a Plaid credit account a
    purchase *and* a payment are both reported as a **positive** amount — the sign does not
    distinguish them, the name does (ticket 0065)."""
    repo.add_plaid_transaction(
        transaction_id=f"pt_{uuid.uuid4().hex}",
        plaid_item_id=ITEM,
        plaid_account_id=CARD,
        plaid_transaction_id=f"tx_{uuid.uuid4().hex}",
        change_type="added",
        amount=Decimal(plaid_amount),
        date=day,
        name=name,
    )


def _seed_card_account_activity(repo) -> None:
    """The real Plaid Sandbox `user_good` shape (measured 2026-08-15 against the Sandbox API): the
    card's charges and its `AUTOMATIC PAYMENT` land on the **card account itself**, not on checking,
    and there is *no* card payment on the checking stream. Four cycles → four observed payments,
    past the classifier's three-cycle floor. The shape `hh_demo_plaid` refused UNKNOWN on."""
    for month in (1, 2, 3, 4):
        # Charges (Plaid-positive), a handful across the cycle.
        _card_acct_txn(
            repo, day=dt.date(2026, month, 5), plaid_amount="500.00", name="Madison Bicycle Shop"
        )
        _card_acct_txn(repo, day=dt.date(2026, month, 7), plaid_amount="500.00", name="KFC")
        _card_acct_txn(repo, day=dt.date(2026, month, 9), plaid_amount="500.00", name="Tectra Inc")
        _card_acct_txn(
            repo, day=dt.date(2026, month, 12), plaid_amount="78.50", name="Touchstone Climbing"
        )
        # The payment, recorded on the card ledger (Plaid-positive, like the charges).
        _card_acct_txn(
            repo,
            day=dt.date(2026, month, 22),
            plaid_amount="1578.50",
            name="AUTOMATIC PAYMENT - THANK YOU",
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
        # Attest the household's **linked** card set (its Plaid credit account, via
        # `current_card_ids`) so coverage can clear — a linked household has no engine `cards` rows,
        # and the card-payment txn maps to that same card so nothing is an UNMATCHED_PAYMENT.
        repo.add_attestation(card_fingerprint=card_fingerprint(current_card_ids(repo)))
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
            # The linked card set is attestable now (reconciled: `current_card_ids` reads the Plaid
            # credit account, not the empty engine `cards` table).
            assert attested_for(repo) is True
            history = linked_history(repo)
            decided = live_decision(repo, history, history.end)

        # A real decision, from real ingested data — a sweep or an honest refusal, never a crash.
        assert decided.decision.action in (Action.SWEEP, Action.REFUSE)
        assert decided.decision.reasons  # it explains itself
        # The attestation cleared coverage — not stuck at UNATTESTED on a linked household.
        assert decided.snapshot.portfolio.coverage is CoverageState.COMPLETE
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


def _seed_checking_no_card_payment(repo) -> None:
    """The same checking history as `_seed_transactions`, but with **no card payment on checking** —
    the honest `user_good` shape, where the card is paid on the card ledger (see
    `_seed_card_account_activity`). This keeps the test from flattering itself: card behavior can
    only be resolved from the *card* account, exactly as ticket 0065 requires."""
    day = dt.date(2026, 1, 2)
    while day <= START + dt.timedelta(days=DAYS):
        _txn(repo, day=day, plaid_amount="-2600.00", name="ACH Electronic CreditGUSTO PAY")
        day += dt.timedelta(days=14)
    for month in (1, 2, 3, 4):
        _txn(repo, day=dt.date(2026, month, 1), plaid_amount="1800.00", name="TENANT RENT")
        _txn(repo, day=dt.date(2026, month, 9), plaid_amount="42.00", name="Starbucks")
        _txn(repo, day=dt.date(2026, month, 22), plaid_amount="63.00", name="McDonald's")


class TestCardAccountActivityUnblocksTheDecision:
    """Ticket 0065: a linked household's card transactions must be attributed to its card, or its
    `behavior` stays UNKNOWN, `CARD_BEHAVIOR_UNKNOWN` blocks `decide()` before the forecast, and it
    refuses forever with a null `projected_low_balance`."""

    @pytest.fixture
    def linked_realcard(self, db, app_engine: Engine):
        with db.begin():
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
            _seed_checking_no_card_payment(repo)  # no card payment on the checking stream
            _seed_card_account_activity(repo)  # charges + AUTOMATIC PAYMENT on the card account
            repo.add_attestation(card_fingerprint=card_fingerprint(current_card_ids(repo)))
        return HH

    def test_card_txns_are_attributed_and_the_engine_forecasts(
        self, linked_realcard, app_engine: Engine
    ) -> None:
        with repository(app_engine, HH) as repo:
            history = linked_history(repo)
            decided = live_decision(repo, history, history.end)

        # The card account's own transactions are attributed to that card — charges *and* the
        # payments the classifier needs (four cycles, past the three-cycle floor). Before 0065 both
        # were dropped: zero card txns in the history.
        assert any(t.kind is TxnKind.CARD_CHARGE and t.card_id == CARD for t in history.txns), (
            "card charges were not attributed to the card"
        )
        payments = [t for t in history.txns if t.kind is TxnKind.CARD_PAYMENT and t.card_id == CARD]
        assert len(payments) >= 3, f"expected the card's own payments, got {len(payments)}"

        # So at least one card resolves to a real behavior instead of UNKNOWN...
        assert any(
            c.behavior is not PaymentBehavior.UNKNOWN for c in decided.snapshot.portfolio.cards
        ), "card behavior stayed UNKNOWN — the engine is still starved"

        # ...and the engine gets *past* the blocking CARD_BEHAVIOR_UNKNOWN refusal to actually
        # forecast: a decision with a non-null projected low, not a blank blocking refusal.
        assert decided.decision.projected_low_balance is not None
        assert decided.decision.action in (Action.SWEEP, Action.REFUSE)

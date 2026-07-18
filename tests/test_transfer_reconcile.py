"""Reconciliation — the poll that backs up the webhooks (ticket 0042, U4).

Proves the backstop: a transfer that never got a webhook is still advanced by the poll, and a
terminal one is left alone.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import text

from backend.transfer import ShadowProvider, TransferIntent
from backend.transfer.provider import Auth, LedgerState, ProviderRef, Return
from backend.transfer.reconcile import run_reconcile
from backend.transfer.saga import submit_leg
from tests.conftest import requires_db

pytestmark = requires_db

ALICE = "alice"


class _SettleProvider:
    def authorize(self, intent: TransferIntent) -> Auth:  # pragma: no cover
        return Auth(intent=intent, idempotency_key="k")

    def submit(self, auth: Auth) -> ProviderRef:  # pragma: no cover
        return ProviderRef("x")

    def status(self, ref: ProviderRef) -> LedgerState:
        return "settled"

    def handle_return(self, ref: ProviderRef) -> Return | None:
        return None


def _intent() -> TransferIntent:
    return TransferIntent(
        household_id=ALICE,
        target_card_id="card-1",
        decision_id="dec-1",
        decision_date=dt.date(2026, 3, 2),
        leg="debit",
        direction="debit",
        amount=Decimal("50.00"),
        provider="increase",
    )


def test_the_poll_advances_a_transfer_that_never_got_a_webhook(db, app_engine) -> None:
    with db.begin():
        db.execute(text("INSERT INTO households (id, archetype) VALUES (:h, 'test')"), {"h": ALICE})
    submit_leg(app_engine, _intent(), ShadowProvider())  # left at submitted, no webhook arrives

    advanced = run_reconcile(app_engine, {"increase": _SettleProvider()})
    assert advanced == 1

    with db.begin():
        states = db.execute(text("SELECT state FROM transfers ORDER BY seq")).scalars().all()
    assert states == ["authorized", "submitted", "settled"]


def test_the_poll_leaves_a_terminal_transfer_alone(db, app_engine) -> None:
    with db.begin():
        db.execute(text("INSERT INTO households (id, archetype) VALUES (:h, 'test')"), {"h": ALICE})
    submit_leg(app_engine, _intent(), ShadowProvider())
    run_reconcile(app_engine, {"increase": _SettleProvider()})  # → settled

    # A second pass has nothing non-terminal to chase.
    assert run_reconcile(app_engine, {"increase": _SettleProvider()}) == 0


def test_the_poll_skips_a_provider_it_was_not_given(db, app_engine) -> None:
    with db.begin():
        db.execute(text("INSERT INTO households (id, archetype) VALUES (:h, 'test')"), {"h": ALICE})
    submit_leg(app_engine, _intent(), ShadowProvider())  # provider='increase'

    # Only a Method adapter supplied — the increase transfer is not touched.
    assert run_reconcile(app_engine, {"method": _SettleProvider()}) == 0

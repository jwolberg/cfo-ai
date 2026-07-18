"""The two-leg sweep orchestration and its timing model (ticket 0045, ADR-0007).

Wait-for-clear (default) gates the Method payoff on the debit reaching `settled`; prefund fires the
payoff immediately. Driven with `ShadowProvider` (settles instantly, moves nothing), so this proves
the sequencing, not a vendor.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import text

from backend.transfer import ShadowProvider
from backend.transfer.saga import Sweep, run_sweep, settle_and_continue
from tests.conftest import requires_db

pytestmark = requires_db

ALICE = "alice"
D1 = dt.date(2026, 3, 2)


def _sweep() -> Sweep:
    return Sweep(
        household_id=ALICE,
        target_card_id="card-1",
        decision_id="dec-1",
        decision_date=D1,
        amount=Decimal("200.00"),
    )


def _household(db) -> None:
    with db.begin():
        db.execute(text("INSERT INTO households (id, archetype) VALUES (:h, 'test')"), {"h": ALICE})


def _rows(db) -> list[tuple[str, str]]:
    """(leg, state) for the household, in ledger order."""
    with db.begin():
        return [
            (r.leg, r.state)
            for r in db.execute(
                text("SELECT leg, state FROM transfers WHERE household_id = :h ORDER BY seq"),
                {"h": ALICE},
            )
        ]


def test_wait_for_clear_holds_the_payoff_until_the_debit_settles(db, app_engine) -> None:
    _household(db)
    debit, payoff = ShadowProvider(), ShadowProvider()

    ref = run_sweep(app_engine, _sweep(), debit_adapter=debit, payoff_adapter=payoff)
    assert ref is not None
    # Only the debit leg so far — the payoff is gated on settlement.
    assert _rows(db) == [("debit", "authorized"), ("debit", "submitted")]

    # A webhook/poll reports the debit settled → the payoff fires.
    settle_and_continue(app_engine, _sweep(), ref, debit_adapter=debit, payoff_adapter=payoff)
    assert _rows(db) == [
        ("debit", "authorized"),
        ("debit", "submitted"),
        ("debit", "settled"),
        ("payoff", "authorized"),
        ("payoff", "submitted"),
    ]


def test_prefund_fires_the_payoff_immediately(db, app_engine) -> None:
    _household(db)
    debit, payoff = ShadowProvider(), ShadowProvider()

    run_sweep(app_engine, _sweep(), debit_adapter=debit, payoff_adapter=payoff, prefund=True)
    rows = _rows(db)
    # The payoff is submitted while the debit is still only submitted (not yet settled).
    assert ("payoff", "submitted") in rows
    assert ("debit", "settled") not in rows
    assert [leg for leg, _ in rows] == ["debit", "debit", "payoff", "payoff"]


def test_settle_and_continue_is_idempotent_on_a_redelivered_settlement(db, app_engine) -> None:
    """A redelivered 'debit settled' must not create a second payoff — the slot guard holds."""
    _household(db)
    debit, payoff = ShadowProvider(), ShadowProvider()
    ref = run_sweep(app_engine, _sweep(), debit_adapter=debit, payoff_adapter=payoff)

    settle_and_continue(app_engine, _sweep(), ref, debit_adapter=debit, payoff_adapter=payoff)
    settle_and_continue(app_engine, _sweep(), ref, debit_adapter=debit, payoff_adapter=payoff)

    rows = _rows(db)
    assert [state for leg, state in rows if leg == "payoff"].count("submitted") == 1

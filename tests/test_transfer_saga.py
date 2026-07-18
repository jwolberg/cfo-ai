"""The money-movement saga (ticket 0042, U4).

The highest-stakes test in the repo lives here: two concurrent submits on one slot must produce
**one** debit, because a duplicate sweep is an overdraft (KTD-2). Plus the status advance through
the SECURITY DEFINER lookup, and that terminal transfers are never re-advanced.
"""

from __future__ import annotations

import datetime as dt
import threading
from decimal import Decimal

from sqlalchemy import text

from backend.db.session import household_scope
from backend.transfer import ShadowProvider, TransferIntent
from backend.transfer.provider import Auth, LedgerState, ProviderRef, Return
from backend.transfer.saga import apply_status, submit_leg
from tests.conftest import requires_db

pytestmark = requires_db

ALICE = "alice"


def _intent(household: str = ALICE) -> TransferIntent:
    return TransferIntent(
        household_id=household,
        target_card_id="card-1",
        decision_id="dec-1",
        decision_date=dt.date(2026, 3, 2),
        leg="debit",
        direction="debit",
        amount=Decimal("50.00"),
        provider="increase",
    )


def _household(db, hid: str = ALICE) -> None:
    with db.begin():
        db.execute(text("INSERT INTO households (id, archetype) VALUES (:h, 'test')"), {"h": hid})


def _states(db, hid: str = ALICE) -> list[str]:
    with db.begin():
        return (
            db.execute(
                text("SELECT state FROM transfers WHERE household_id = :h ORDER BY seq"), {"h": hid}
            )
            .scalars()
            .all()
        )


def test_submit_leg_writes_authorized_then_submitted(db, app_engine) -> None:
    _household(db)
    ref = submit_leg(app_engine, _intent(), ShadowProvider())
    assert ref is not None
    assert _states(db) == ["authorized", "submitted"]


def test_a_second_submit_on_the_same_slot_is_refused(db, app_engine) -> None:
    """Sequential duplicate: the slot already reached submitted, so the second returns None and
    creates no second debit."""
    _household(db)
    first = submit_leg(app_engine, _intent(), ShadowProvider())
    second = submit_leg(app_engine, _intent(), ShadowProvider())
    assert first is not None
    assert second is None
    assert _states(db).count("submitted") == 1


def test_two_concurrent_submits_produce_exactly_one_debit(db, app_engine) -> None:
    """THE test. Two threads race the same slot; the advisory lock serializes them and exactly one
    reaches submitted. A duplicate here is an overdraft."""
    _household(db)
    barrier = threading.Barrier(2)
    results: list[str | None] = []
    lock = threading.Lock()

    def _run() -> None:
        barrier.wait()  # start both as close to simultaneously as possible
        outcome = submit_leg(app_engine, _intent(), ShadowProvider())
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for r in results if r is not None) == 1, "both threads submitted — a double debit"
    assert _states(db).count("submitted") == 1


class _StatusProvider:
    """A fake that reports a chosen status and never a return."""

    def __init__(self, state: LedgerState) -> None:
        self._state = state

    def authorize(self, intent: TransferIntent) -> Auth:  # pragma: no cover - unused here
        return Auth(intent=intent, idempotency_key="k")

    def submit(self, auth: Auth) -> ProviderRef:  # pragma: no cover
        return ProviderRef("x")

    def status(self, ref: ProviderRef) -> LedgerState:
        return self._state

    def handle_return(self, ref: ProviderRef) -> Return | None:
        return None


class _ReturningProvider(_StatusProvider):
    def __init__(self, code: str) -> None:
        super().__init__("returned")
        self._code = code

    def handle_return(self, ref: ProviderRef) -> Return | None:
        return Return(return_code=self._code, state="returned")


def _submitted_ref(db, app_engine) -> str:
    _household(db)
    return submit_leg(app_engine, _intent(), ShadowProvider())  # type: ignore[return-value]


def test_apply_status_advances_submitted_to_settled(db, app_engine) -> None:
    ref = _submitted_ref(db, app_engine)
    new_state = apply_status(app_engine, _StatusProvider("settled"), "increase", ref)
    assert new_state == "settled"
    assert _states(db) == ["authorized", "submitted", "settled"]


def test_apply_status_is_idempotent_when_the_state_is_unchanged(db, app_engine) -> None:
    ref = _submitted_ref(db, app_engine)
    apply_status(app_engine, _StatusProvider("settled"), "increase", ref)
    apply_status(app_engine, _StatusProvider("settled"), "increase", ref)  # terminal, no re-advance
    assert _states(db).count("settled") == 1


def test_apply_status_records_a_return_with_its_code(db, app_engine) -> None:
    ref = _submitted_ref(db, app_engine)
    new_state = apply_status(app_engine, _ReturningProvider("R01"), "increase", ref)
    assert new_state == "returned"
    with app_engine.connect() as conn, household_scope(conn, ALICE) as scoped:
        code = scoped.execute(
            text("SELECT return_code FROM transfers WHERE state = 'returned'")
        ).scalar()
    assert code == "R01"


def test_apply_status_on_an_unknown_ref_is_a_noop(db, app_engine) -> None:
    _household(db)
    assert apply_status(app_engine, _StatusProvider("settled"), "increase", "nope") is None

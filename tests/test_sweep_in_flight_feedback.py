"""The SWEEP_IN_FLIGHT feedback: an in-flight transfer suppresses the next sweep (ticket 0043, U5).

The engine already models the state and refuses on it (`tests/test_decide.py`); nothing produced it,
because `assemble_snapshot` hardcoded `ZERO`. U5 closes the loop: `Repository.sweeps_in_flight` sums
the unsettled debit legs, and that value, fed to the snapshot, makes `decide()` refuse. This proves
the loop end to end — the ledger value becoming an engine refusal.
"""

from __future__ import annotations

import datetime as dt
import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import text

from backend.db.repository import repository
from backend.precompute import DEMO_POLICY, DEMO_SPEC, SEED, WINDOW_START, assemble_snapshot
from backend.transfer import ShadowProvider, TransferIntent
from backend.transfer.saga import submit_leg
from engine.decide import decide
from engine.models import ReasonCode
from sim.household import generate
from tests.conftest import requires_db

pytestmark = requires_db

ALICE, BOB = "alice", "bob"


def _household(db, hid: str) -> None:
    with db.begin():
        db.execute(text("INSERT INTO households (id, archetype) VALUES (:h, 'test')"), {"h": hid})


def _add(repo, *, state: str, amount: str, decision_date: dt.date, leg: str = "debit") -> None:
    repo.add_transfer(
        transfer_id=uuid.uuid4().hex,
        target_card_id="card-1",
        decision_id=f"dec-{decision_date.isoformat()}-{state}",
        decision_date=decision_date,
        leg=leg,
        state=state,
        direction="debit" if leg == "debit" else "credit",
        amount=Decimal(amount),
        provider="increase" if leg == "debit" else "method",
        idempotency_key=f"{decision_date.isoformat()}-{leg}",
    )


D1 = dt.date(2026, 3, 2)
D2 = dt.date(2026, 3, 9)


def test_no_transfers_means_nothing_in_flight(db, app_engine) -> None:
    _household(db, ALICE)
    with repository(app_engine, ALICE) as repo:
        assert repo.sweeps_in_flight() == Decimal("0")


def test_a_submitted_debit_is_in_flight(db, app_engine) -> None:
    _household(db, ALICE)
    with repository(app_engine, ALICE) as repo:
        _add(repo, state="submitted", amount="200.00", decision_date=D1)
        assert repo.sweeps_in_flight() == Decimal("200.00")


def test_two_in_flight_slots_sum(db, app_engine) -> None:
    _household(db, ALICE)
    with repository(app_engine, ALICE) as repo:
        _add(repo, state="submitted", amount="200.00", decision_date=D1)
        _add(repo, state="pending", amount="150.00", decision_date=D2)
        assert repo.sweeps_in_flight() == Decimal("350.00")


def test_a_settled_debit_is_not_in_flight(db, app_engine) -> None:
    """The slot's latest debit state is settled, so it drops out of the sum — the sweep landed."""
    _household(db, ALICE)
    with repository(app_engine, ALICE) as repo:
        _add(repo, state="submitted", amount="200.00", decision_date=D1)
        _add(repo, state="settled", amount="200.00", decision_date=D1)  # later seq wins
        assert repo.sweeps_in_flight() == Decimal("0")


def test_a_returned_debit_unwinds(db, app_engine) -> None:
    _household(db, ALICE)
    with repository(app_engine, ALICE) as repo:
        _add(repo, state="submitted", amount="200.00", decision_date=D1)
        _add(repo, state="returned", amount="200.00", decision_date=D1)
        assert repo.sweeps_in_flight() == Decimal("0")


def test_the_payoff_leg_is_not_double_counted(db, app_engine) -> None:
    """Only the debit leg is money leaving checking; a payoff row adds nothing to the sum."""
    _household(db, ALICE)
    with repository(app_engine, ALICE) as repo:
        _add(repo, state="submitted", amount="200.00", decision_date=D1, leg="debit")
        _add(repo, state="submitted", amount="200.00", decision_date=D1, leg="payoff")
        assert repo.sweeps_in_flight() == Decimal("200.00")


def test_one_households_in_flight_is_invisible_to_another(db, app_engine) -> None:
    _household(db, ALICE)
    _household(db, BOB)
    with repository(app_engine, ALICE) as repo:
        _add(repo, state="submitted", amount="200.00", decision_date=D1)
    with repository(app_engine, BOB) as repo:
        assert repo.sweeps_in_flight() == Decimal("0")


def test_an_in_flight_sweep_makes_the_next_decision_refuse(db, app_engine) -> None:
    """The loop, closed: submit a debit (via the saga), read the ledger's in-flight value, feed it
    to a real snapshot, and watch `decide()` refuse with SWEEP_IN_FLIGHT."""
    _household(db, ALICE)
    submit_leg(
        app_engine,
        TransferIntent(
            household_id=ALICE,
            target_card_id="card-1",
            decision_id="dec-1",
            decision_date=D1,
            leg="debit",
            direction="debit",
            amount=Decimal("200.00"),
            provider="increase",
        ),
        ShadowProvider(),
    )
    with repository(app_engine, ALICE) as repo:
        in_flight = repo.sweeps_in_flight()
    assert in_flight == Decimal("200.00")

    history = generate(DEMO_SPEC, WINDOW_START, 150, seed=SEED)
    snapshot = assemble_snapshot(
        history=history,
        today=WINDOW_START + timedelta(days=90),
        spec=DEMO_SPEC,
        policy=DEMO_POLICY,
        ledger_balances={DEMO_SPEC.card.card_id: Decimal("14000.00")},
        checking=Decimal("3200.00"),
        sweeps_in_flight=in_flight,
    )
    assert snapshot.sweeps_in_flight == Decimal("200.00")
    assert decide(snapshot).has(ReasonCode.SWEEP_IN_FLIGHT)

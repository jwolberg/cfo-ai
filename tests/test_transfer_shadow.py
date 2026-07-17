"""The `TransferProvider` port and `ShadowProvider` (ticket 0040, U2).

The port's shape and the shadow provider's "moves nothing" guarantee are proven here. Two of these
need no database — the port is types, and the provider touches no network — so they run always; the
ledger-trail tests need a real Postgres and carry `requires_db`.
"""

from __future__ import annotations

import datetime as dt
import socket
from decimal import Decimal

import pytest
from sqlalchemy import text

from backend.db.repository import repository
from backend.transfer import (
    Auth,
    ProviderRef,
    ShadowProvider,
    TransferIntent,
    TransferProvider,
    run_shadow_sweep,
)
from tests.conftest import requires_db


def _intent(leg: str = "debit", provider: str = "increase") -> TransferIntent:
    return TransferIntent(
        household_id="h1",
        target_card_id="card-1",
        decision_id="dec-1",
        decision_date=dt.date(2026, 3, 2),
        leg=leg,  # type: ignore[arg-type]
        direction="debit" if leg == "debit" else "credit",  # type: ignore[arg-type]
        amount=Decimal("50.00"),
        provider=provider,  # type: ignore[arg-type]
    )


def test_the_shadow_provider_satisfies_the_port_structurally() -> None:
    """`@runtime_checkable`: the shadow provider — and any stub adapter with the four methods —
    satisfies `TransferProvider` by shape, which is what lets the saga accept either."""
    assert isinstance(ShadowProvider(), TransferProvider)

    class _StubAdapter:
        def authorize(self, intent):  # noqa: ANN001, ANN201
            ...

        def submit(self, auth):  # noqa: ANN001, ANN201
            ...

        def status(self, ref):  # noqa: ANN001, ANN201
            ...

        def handle_return(self, ref):  # noqa: ANN001, ANN201
            ...

    assert isinstance(_StubAdapter(), TransferProvider)

    class _MissingOne:
        def authorize(self, intent):  # noqa: ANN001, ANN201
            ...

    assert not isinstance(_MissingOne(), TransferProvider)


def test_the_shadow_provider_touches_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under a socket that refuses every connection, the whole port still runs — proof the provider
    calls no vendor. `submit` is the one that matters: the point of no return, here a logged no-op
    returning a synthetic ref."""

    def _no_connect(*_a, **_k):
        raise AssertionError("the shadow provider opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", _no_connect)

    provider = ShadowProvider()
    auth = provider.authorize(_intent())
    assert isinstance(auth, Auth)
    ref = provider.submit(auth)
    assert isinstance(ref, ProviderRef)
    assert ref.provider_transfer_id.startswith("shadow-"), "a real submit would return a vendor id"
    assert provider.status(ref) == "settled"
    assert provider.handle_return(ref) is None


def test_the_idempotency_key_is_the_slot_plus_step() -> None:
    """KTD-4: derived from `(household, decision_date)` and the step, so a retry reuses it."""
    auth = ShadowProvider().authorize(_intent(leg="payoff"))
    assert auth.idempotency_key == "h1-2026-03-02-payoff-submit"


@requires_db
def test_a_shadow_sweep_appends_the_ledger_trail_in_order(db, app_engine) -> None:
    """A full shadow leg writes authorized → submitted → settled to the append-only ledger, moving
    no money, and every row is scoped to the household and carries the intent's amount."""
    with db.begin():
        db.execute(text("INSERT INTO households (id, archetype) VALUES ('h1', 'test')"))

    with repository(app_engine, "h1") as repo:
        states = run_shadow_sweep(repo, _intent())
        rows = repo.transfers()

    assert states == ["authorized", "submitted", "settled"]
    assert [r["state"] for r in rows] == ["authorized", "submitted", "settled"]
    assert {r["household_id"] for r in rows} == {"h1"}
    assert {r["amount"] for r in rows} == {Decimal("50.00")}
    # The submitted/settled rows carry the synthetic shadow ref; nothing real was created.
    refs = {r["provider_transfer_id"] for r in rows if r["provider_transfer_id"]}
    assert all(ref.startswith("shadow-") for ref in refs)


@requires_db
def test_a_forced_return_appends_a_returned_row(db, app_engine) -> None:
    """`force_return` is the path U6 drives against a real sandbox reversal: the trail ends at a
    `returned` row carrying the code, not at `settled`."""
    with db.begin():
        db.execute(text("INSERT INTO households (id, archetype) VALUES ('h1', 'test')"))

    with repository(app_engine, "h1") as repo:
        states = run_shadow_sweep(repo, _intent(), force_return="R01")
        rows = repo.transfers()

    assert states == ["authorized", "submitted", "returned"]
    returned = [r for r in rows if r["state"] == "returned"]
    assert len(returned) == 1
    assert returned[0]["return_code"] == "R01"

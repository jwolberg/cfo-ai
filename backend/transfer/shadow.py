"""`ShadowProvider` and the shadow sweep — the first shipped state, moving nothing (0040, U2).

`architecture.md` [6] and `prd.md`'s roadmap both put shadow mode first: *"Ingest → normalize →
snapshot → decide → log. Move nothing."* `ShadowProvider` is that: it implements the full
`TransferProvider` port, logs what it *would* do, and calls no vendor — `submit()` returns a
synthetic ref rather than creating a transfer. `run_shadow_sweep` drives one leg through the port
and writes the append-only ledger trail (U1), so the saga's logic and the engine's decisions can be
graded against real households before a dollar is ever at risk.

When the real saga lands (U4) it owns the ledger append and orchestrates the live adapters; the
shadow path fuses provider + orchestration here only because it has no vendor round-trip to
coordinate. The provider itself touches no network — proven in `tests/test_transfer_shadow.py` under
a socket that refuses to connect.
"""

from __future__ import annotations

import logging
import uuid

from backend.db.repository import Repository
from backend.transfer.provider import (
    Auth,
    LedgerState,
    ProviderRef,
    Return,
    TransferIntent,
)

log = logging.getLogger(__name__)


def _idempotency_key(intent: TransferIntent) -> str:
    """The vendor Idempotency-Key, derived from the `(household, decision_date)` slot and the step
    (KTD-4). One key per leg-submission, reused on every retry so a crash cannot double-submit."""
    return f"{intent.household_id}-{intent.decision_date.isoformat()}-{intent.leg}-submit"


class ShadowProvider:
    """A `TransferProvider` that moves no money. Every method logs; `submit` calls no vendor and
    returns a synthetic ref. This is what runs against real households to produce the tail-risk
    number before either real SDK is wired."""

    def authorize(self, intent: TransferIntent) -> Auth:
        key = _idempotency_key(intent)
        log.info(
            "shadow authorize: household=%s leg=%s amount=%s key=%s",
            intent.household_id,
            intent.leg,
            intent.amount,
            key,
        )
        return Auth(intent=intent, idempotency_key=key)

    def submit(self, auth: Auth) -> ProviderRef:
        # The logged no-op. A real submit would create a transfer at the vendor; here it does not,
        # and the synthetic ref makes that unmistakable to anything downstream.
        ref = ProviderRef(provider_transfer_id=f"shadow-{auth.idempotency_key}")
        log.info(
            "shadow submit (no-op): would submit %s -> %s",
            auth.intent.leg,
            ref.provider_transfer_id,
        )
        return ref

    def status(self, ref: ProviderRef) -> LedgerState:
        # Shadow settles immediately — there is no vendor to wait on.
        return "settled"

    def handle_return(self, ref: ProviderRef) -> Return | None:
        # A shadow transfer never spontaneously returns; a forced return is driven explicitly
        # (see `run_shadow_sweep(force_return=...)` and U6's sandbox hard gate).
        return None


def run_shadow_sweep(
    repo: Repository,
    intent: TransferIntent,
    *,
    provider: ShadowProvider | None = None,
    force_return: str | None = None,
) -> list[LedgerState]:
    """Drive one leg through the port in shadow mode, writing the append-only ledger trail.

    Returns the ledger states appended, in order. Moves no money: the provider's `submit` is a
    no-op. `force_return` appends a `returned` row carrying that code instead of settling — U6's
    hard gate exercises against a real sandbox reversal.

    The transfer must be written through `repo`, so RLS binds every row to `intent.household_id`.
    """
    provider = provider or ShadowProvider()
    states: list[LedgerState] = []

    def append(
        state: LedgerState,
        *,
        provider_transfer_id: str | None = None,
        return_code: str | None = None,
    ) -> None:
        repo.add_transfer(
            transfer_id=uuid.uuid4().hex,
            target_card_id=intent.target_card_id,
            decision_id=intent.decision_id,
            decision_date=intent.decision_date,
            leg=intent.leg,
            state=state,
            direction=intent.direction,
            amount=intent.amount,
            provider=intent.provider,
            idempotency_key=_idempotency_key(intent),
            provider_transfer_id=provider_transfer_id,
            return_code=return_code,
        )
        states.append(state)

    auth = provider.authorize(intent)
    append("authorized")

    ref = provider.submit(auth)
    append("submitted", provider_transfer_id=ref.provider_transfer_id)

    if force_return is not None:
        append("returned", provider_transfer_id=ref.provider_transfer_id, return_code=force_return)
        return states

    append(provider.status(ref), provider_transfer_id=ref.provider_transfer_id)
    return states

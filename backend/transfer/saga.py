"""The money-movement saga (ticket 0042, U4).

`architecture.md` [5]'s state machine, built on the repo's proven Cloud Tasks + row-lock durability
pattern (the Plaid sync worker) rather than Temporal — Temporal enters at the trigger that turns
`submit()` on, and this rung moves no money (Decision 1).

Two operations the worker and the reconciliation poll call:

- **`submit_leg`** — the highest-stakes path. A duplicate submit is an overdraft, so it is guarded
  by a Postgres **advisory transaction lock on the `(household, decision_date, leg)` slot** plus a
  check that the slot has not already reached `submitted`. The lock (not a UNIQUE) serializes two
  concurrent first-submits — the race append-only corrections rule a constraint out of (KTD-2).
- **`apply_status`** — advance a transfer as a webhook or the poll reports it. Resolves the
  household through the `SECURITY DEFINER` lookup (a webhook names a provider ref, not a household;
  ADR-0006), scopes, takes `FOR UPDATE` on the transfer's rows to serialize webhook-vs-poll, and
  appends the new state only when it changed. Terminal states are never re-advanced.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.db.repository import Repository
from backend.db.session import household_scope
from backend.transfer.provider import LedgerState, ProviderRef, TransferIntent, TransferProvider

# Once a transfer reaches one of these, no webhook or poll advances it further.
TERMINAL: frozenset[LedgerState] = frozenset({"settled", "returned", "failed", "cancelled"})


def _slot_key(intent: TransferIntent) -> str:
    return f"{intent.household_id}:{intent.decision_date.isoformat()}:{intent.leg}"


def submit_leg(engine: Engine, intent: TransferIntent, provider: TransferProvider) -> str | None:
    """Authorize and submit one leg, idempotently. Returns the provider transfer id, or `None` if
    the slot already reached `submitted` (a duplicate the guard refused).

    The advisory lock is held across `provider.submit` on purpose: the critical section is exactly
    "decide to submit, then submit", and releasing before the vendor call would reopen the race.
    """
    with engine.connect() as conn, household_scope(conn, intent.household_id) as scoped:
        # Serialize every actor on this slot — including two first-submits that would each find the
        # slot empty. hashtext maps the slot key to the lock's integer space.
        scoped.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": _slot_key(intent)})

        already = scoped.execute(
            text(
                "SELECT count(*) FROM transfers WHERE household_id = :h AND decision_date = :d"
                " AND leg = :leg AND state = 'submitted'"
            ),
            {"h": intent.household_id, "d": intent.decision_date, "leg": intent.leg},
        ).scalar()
        if already:
            return None  # the slot already reached submitted — do not create a second debit

        repo = Repository(conn=scoped, household_id=intent.household_id)
        auth = provider.authorize(intent)

        def _append(state: LedgerState, *, ref: str | None = None) -> None:
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
                idempotency_key=auth.idempotency_key,
                provider_transfer_id=ref,
            )

        _append("authorized")
        ref = provider.submit(auth)
        _append("submitted", ref=ref.provider_transfer_id)
        return ref.provider_transfer_id


def _household_for(engine: Engine, provider_name: str, provider_transfer_id: str) -> str | None:
    """Map `(provider, ref) → household` through the SECURITY DEFINER lookup — the one read of the
    FORCE'd `transfers` table allowed before a scope is set (ADR-0006)."""
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT transfer_household_for_provider_ref(:p, :r)"),
            {"p": provider_name, "r": provider_transfer_id},
        ).scalar()


def apply_status(
    engine: Engine,
    provider: TransferProvider,
    provider_name: str,
    provider_transfer_id: str,
) -> LedgerState | None:
    """Advance a transfer to the state the provider now reports. Returns the new (or unchanged)
    state, or `None` if the ref is unknown. Idempotent: a redelivered webhook that reports the same
    state appends nothing, and a terminal transfer is never re-advanced."""
    household = _household_for(engine, provider_name, provider_transfer_id)
    if household is None:
        return None

    with engine.connect() as conn, household_scope(conn, household) as scoped:
        # Serialize webhook-vs-poll on this transfer with an advisory lock, not `FOR UPDATE` — the
        # ledger is append-only (cfo_app holds SELECT+INSERT only, so it cannot lock a row), and the
        # lock needs no row to exist. Held to transaction end, like submit_leg's.
        scoped.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:r))"), {"r": provider_transfer_id}
        )
        rows = (
            scoped.execute(
                text("SELECT * FROM transfers WHERE provider_transfer_id = :r ORDER BY seq DESC"),
                {"r": provider_transfer_id},
            )
            .mappings()
            .all()
        )
        if not rows:
            return None
        latest = rows[0]
        if latest["state"] in TERMINAL:
            return latest["state"]

        ref = ProviderRef(provider_transfer_id=provider_transfer_id)
        returned = provider.handle_return(ref)
        if returned is not None:
            new_state, return_code = returned.state, returned.return_code
        else:
            new_state, return_code = provider.status(ref), None

        if new_state == latest["state"]:
            return new_state

        Repository(conn=scoped, household_id=household).add_transfer(
            transfer_id=uuid.uuid4().hex,
            target_card_id=latest["target_card_id"],
            decision_id=latest["decision_id"],
            decision_date=latest["decision_date"],
            leg=latest["leg"],
            state=new_state,
            direction=latest["direction"],
            amount=latest["amount"],
            provider=latest["provider"],
            idempotency_key=latest["idempotency_key"],
            provider_transfer_id=provider_transfer_id,
            return_code=return_code,
        )
        return new_state

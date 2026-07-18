"""Reconciliation — the poll that backs up the webhooks (ticket 0042, U4).

Webhooks are not sufficient alone (KTD-5): Increase exhausts its retries after ~72h, and Method
disables a webhook after repeated failures. So a periodic poll walks every non-terminal transfer and
advances it against the provider's own truth — the backstop that catches a webhook the doorbell
never received. Same shape as the Plaid nightly poll: enumerate households (the tenant registry),
scope each, then advance its open transfers through `saga.apply_status`.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.db.session import household_scope
from backend.transfer.provider import TransferProvider
from backend.transfer.saga import TERMINAL, apply_status


def _open_refs(engine: Engine, household_id: str) -> list[tuple[str, str]]:
    """This household's `(provider, provider_transfer_id)` for transfers whose latest state is not
    terminal — the ones the poll still needs to chase."""
    with engine.connect() as conn, household_scope(conn, household_id) as scoped:
        rows = (
            scoped.execute(
                text(
                    "SELECT DISTINCT ON (provider_transfer_id)"
                    " provider, provider_transfer_id, state"
                    " FROM transfers WHERE provider_transfer_id IS NOT NULL"
                    " ORDER BY provider_transfer_id, seq DESC"
                )
            )
            .mappings()
            .all()
        )
    return [(r["provider"], r["provider_transfer_id"]) for r in rows if r["state"] not in TERMINAL]


def run_reconcile(engine: Engine, providers: dict[str, TransferProvider]) -> int:
    """Advance every non-terminal transfer against its provider. Returns the count advanced.

    `providers` maps a provider name (`increase` / `method`) to the adapter that reads its status.
    A ref whose provider is not in the map is skipped — nothing pretends to know a vendor it was not
    given an adapter for.
    """
    advanced = 0
    with engine.connect() as conn:
        households = conn.execute(text("SELECT id FROM households")).scalars().all()

    for household_id in households:
        for provider_name, ref in _open_refs(engine, household_id):
            provider = providers.get(provider_name)
            if provider is None:
                continue
            before = _latest_state(engine, household_id, ref)
            after = apply_status(engine, provider, provider_name, ref)
            if after is not None and after != before:
                advanced += 1
    return advanced


def _latest_state(engine: Engine, household_id: str, provider_transfer_id: str) -> str | None:
    with engine.connect() as conn, household_scope(conn, household_id) as scoped:
        return scoped.execute(
            text(
                "SELECT state FROM transfers WHERE provider_transfer_id = :r"
                " ORDER BY seq DESC LIMIT 1"
            ),
            {"r": provider_transfer_id},
        ).scalar()

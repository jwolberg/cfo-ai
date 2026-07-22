"""The cursored `/transactions/sync` loop — the worker end to end.

Two triggers reach the same loop: the Cloud Tasks worker (a webhook fired, U2 enqueued it) and the
nightly Cloud Scheduler poll (every item, regardless of webhooks). Both call `run_sync`, which:

1. **Resolves the household** via the `SECURITY DEFINER` `plaid_household_for_item()` — the one read
   of FORCE'd `plaid_items` allowed before a scope is set (ADR-0005) — then `SET LOCAL` and works
   scoped from there.
2. **Takes `SELECT ... FOR UPDATE` on the item row** before reading the cursor, so the two triggers
   cannot race into a double-insert: a concurrent run blocks until the first commits, then reads the
   *advanced* cursor and finds nothing new.
3. **Loops `/transactions/sync`** from the stored cursor, appending every added/modified/removed as
   a new row, until `has_more` is false. Success advances the cursor and stamps
   `last_successful_sync_at` (which becomes `Account.balance_age_days`). It is atomic: a failure
   mid-loop rolls the whole transaction back, so the cursor never advances past rows that were not
   committed and no partial page is left behind.
4. **On `ITEM_LOGIN_REQUIRED`**, flips `status` to `login_required` in a separate transaction and
   stops — `last_successful_sync_at` untouched, so the freshness gate correctly ages.

Redelivery is a no-op by construction: a second run from the stored cursor returns an empty page.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any

import plaid
from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.db.repository import Repository, repository
from backend.db.session import household_scope
from backend.identity.deps import User, current_real_user
from backend.plaid.accounts import IngestResult, ingest_account_state
from backend.plaid.deps import get_engine, get_oidc_verifier, get_plaid_client

router = APIRouter(prefix="/plaid", tags=["plaid"])

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncResult:
    status: str  # "ok" | "login_required" | "unknown_item"
    added: int = 0
    modified: int = 0
    removed: int = 0


@dataclass(frozen=True)
class RefreshResult:
    """One item brought fully current: its transactions *and* its balances/card terms.

    The two ingests are deliberately separate modules (`sync.py` records what a household *did*,
    `accounts.py` records what is *true now*), but every trigger wants both — a linked household is
    undecidable with only one. `refresh_item` composes them at the trigger boundary so no caller
    can forget the account-state half (the gap that left `/live-decision` unreachable through the
    web Link flow: it drove only `/transactions/sync`, so `plaid_accounts` never populated).
    """

    sync: SyncResult
    accounts: IngestResult


class _LoginRequired(Exception):
    """Raised inside the scoped transaction so it rolls back; the status flip is a separate one."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code


def _sync_request(access_token: str, cursor: str | None):
    from plaid.model.transactions_sync_request import TransactionsSyncRequest

    if cursor:
        return TransactionsSyncRequest(access_token=access_token, cursor=cursor)
    return TransactionsSyncRequest(access_token=access_token)


def _fields(txn: Any) -> dict:
    return txn.to_dict() if hasattr(txn, "to_dict") else dict(txn)


def _append(repo: Repository, plaid_item_id: str, txn: Any, change_type: str) -> None:
    d = _fields(txn)
    amount = d.get("amount")
    repo.add_plaid_transaction(
        transaction_id=str(uuid.uuid4()),
        plaid_item_id=plaid_item_id,
        plaid_account_id=d["account_id"],
        plaid_transaction_id=d["transaction_id"],
        pending_transaction_id=d.get("pending_transaction_id"),
        # Plaid's amount is a float; `Decimal(str(x))` is the only conversion that keeps the cent —
        # `Decimal(0.1)` is 0.1000000000000000055..., which is the bug the NUMERIC column exists to
        # prevent (ADR-0002 [2.2]).
        amount=Decimal(str(amount)) if amount is not None else None,
        date=d.get("date"),
        name=d.get("name"),
        merchant_name=d.get("merchant_name"),
        change_type=change_type,
    )


def _resolve_household(engine: Engine, plaid_item_id: str) -> str | None:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT plaid_household_for_item(:item)"), {"item": plaid_item_id}
        ).scalar()


def _flip_status(engine: Engine, household_id: str, plaid_item_id: str, error_code: str) -> None:
    with engine.connect() as conn, household_scope(conn, household_id) as scoped:
        scoped.execute(
            text(
                "UPDATE plaid_items SET status = 'login_required', error_code = :ec"
                " WHERE plaid_item_id = :item"
            ),
            {"ec": error_code, "item": plaid_item_id},
        )


def _sync_scoped(engine: Engine, household_id: str, plaid_item_id: str, client: Any) -> SyncResult:
    with engine.connect() as conn, household_scope(conn, household_id) as scoped:
        row = scoped.execute(
            text(
                "SELECT access_token, cursor FROM plaid_items"
                " WHERE plaid_item_id = :item FOR UPDATE"
            ),
            {"item": plaid_item_id},
        ).one()
        repo = Repository(conn=scoped, household_id=household_id)
        cursor = row.cursor
        added = modified = removed = 0

        while True:
            try:
                response = client.transactions_sync(_sync_request(row.access_token, cursor))
            except plaid.ApiException as exc:
                code = json.loads(exc.body).get("error_code") if exc.body else None
                if code == "ITEM_LOGIN_REQUIRED":
                    raise _LoginRequired(code) from exc
                raise

            for txn in response.added:
                _append(repo, plaid_item_id, txn, "added")
                added += 1
            for txn in response.modified:
                _append(repo, plaid_item_id, txn, "modified")
                modified += 1
            for txn in response.removed:
                _append(repo, plaid_item_id, txn, "removed")
                removed += 1

            cursor = response.next_cursor
            if not response.has_more:
                break

        scoped.execute(
            text(
                "UPDATE plaid_items SET cursor = :cursor, last_successful_sync_at = now(),"
                " status = 'healthy', error_code = NULL WHERE plaid_item_id = :item"
            ),
            {"cursor": cursor, "item": plaid_item_id},
        )
    return SyncResult("ok", added, modified, removed)


def run_sync(engine: Engine, plaid_item_id: str, client: Any) -> SyncResult:
    """Sync one item end to end. Resolves the household, locks the item, loops, stamps or flips."""
    household_id = _resolve_household(engine, plaid_item_id)
    if household_id is None:
        return SyncResult("unknown_item")
    try:
        return _sync_scoped(engine, household_id, plaid_item_id, client)
    except _LoginRequired as exc:
        _flip_status(engine, household_id, plaid_item_id, exc.error_code)
        return SyncResult("login_required")


def refresh_item(engine: Engine, plaid_item_id: str, client: Any) -> RefreshResult:
    """Bring one item fully current: sync its transactions, then refresh its balances/card terms.

    The account-state half is **fail-soft**: transactions are the priority, and a stumble in
    `/accounts/balance/get` (a Plaid blip, or an Item still warming up) must not fail the sync or,
    for the poll backstop, poison the whole batch — the balances refresh on the next cycle. This
    mirrors `ingest_account_state`'s own posture, which already degrades to "balances only" when the
    `liabilities` product is absent. A transaction sync that itself failed
    (`login_required`/`unknown_item`) skips the refresh: there is nothing fresh to read.
    """
    sync = run_sync(engine, plaid_item_id, client)
    if sync.status != "ok":
        return RefreshResult(sync=sync, accounts=IngestResult(sync.status))
    try:
        accounts = ingest_account_state(engine, plaid_item_id, client)
    except Exception:  # noqa: BLE001 — fail-soft: never let the balance refresh fail the sync
        log.warning("account-state refresh failed for %s; balances not updated", plaid_item_id)
        accounts = IngestResult("error")
    return RefreshResult(sync=sync, accounts=accounts)


def run_poll(engine: Engine, client: Any) -> list[SyncResult]:
    """Sync every item, for every household, regardless of webhooks — the reconciliation backstop.

    Enumerates households (unscoped: `households` has no RLS, it is the tenant registry), then each
    household's items under scope, then hands each item to `run_sync`. Missed webhooks surface here
    "in hours rather than when someone's rent bounces" (`architecture.md` [3.1]).
    """
    with engine.connect() as conn:
        household_ids = [
            r[0] for r in conn.execute(text("SELECT id FROM households WHERE deleted_at IS NULL"))
        ]
    item_ids: list[str] = []
    for household_id in household_ids:
        with engine.connect() as conn, household_scope(conn, household_id) as scoped:
            item_ids.extend(Repository(conn=scoped, household_id=household_id).plaid_item_ids())
    return [refresh_item(engine, item_id, client).sync for item_id in item_ids]


# --- the two triggers, both behind the OIDC gate ------------------------------------------------

OidcVerifier = Callable[[str | None], bool]


@router.post("/sync/worker", response_model=None)
async def sync_worker(
    request: Request,
    engine: Annotated[Engine, Depends(get_engine)],
    client: Annotated[Any, Depends(get_plaid_client)],
    verify: Annotated[OidcVerifier, Depends(get_oidc_verifier)],
) -> Response | dict:
    if not verify(request.headers.get("Authorization")):
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    body = json.loads(await request.body())
    plaid_item_id = body.get("plaid_item_id")
    if not plaid_item_id:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
    refreshed = refresh_item(engine, plaid_item_id, client)
    result = refreshed.sync
    return {
        "status": result.status,
        "added": result.added,
        "modified": result.modified,
        "removed": result.removed,
        "accounts": refreshed.accounts.accounts,
        "liabilities": refreshed.accounts.liabilities,
    }


@router.post("/sync/poll", response_model=None)
async def sync_poll(
    request: Request,
    engine: Annotated[Engine, Depends(get_engine)],
    client: Annotated[Any, Depends(get_plaid_client)],
    verify: Annotated[OidcVerifier, Depends(get_oidc_verifier)],
) -> Response | dict:
    if not verify(request.headers.get("Authorization")):
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    results = run_poll(engine, client)
    return {"items_synced": len(results)}


@router.post("/sync/now", response_model=None)
def sync_now(
    request: Request,
    user: Annotated[User, Depends(current_real_user)],
    engine: Annotated[Engine, Depends(get_engine)],
    client: Annotated[Any, Depends(get_plaid_client)],
) -> dict:
    """Owner-triggered sync of the caller's own household — the session-authed sync trigger.

    The production triggers above are OIDC-gated for Cloud Tasks / Cloud Scheduler: unreachable from
    a browser, and the webhook path needs a public URL Plaid can call. This is their session-authed
    sibling, so a household **owner** can pull their own items on demand — which lets the web Link
    page prove the loop end to end (link → exchange → sync → rows) without any GCP wiring. It
    reuses `_linkable_household`, so the owner + non-demo + single-household checks are identical to
    the exchange it follows; a `viewer` or a non-owner is refused there.

    **`current_real_user`, not `current_user` (ticket 0058).** This is the third user-keyed route,
    and it was missed when the other two were gated because it lives here rather than in `link.py`.
    `_linkable_household` did refuse a demo caller — but only in the body, and `get_plaid_client` is
    a *dependency*, so it is built first: on a deployment with no Plaid credentials (which is
    production today) the public demo session got a **500** out of `PlaidNotConfigured` instead of a
    403, before any gate ran. Measured against the deployed API on 2026-07-22. Ordering the identity
    gate ahead of the client is what makes the refusal independent of whether Plaid is configured.
    """
    from backend.plaid.link import _linkable_household

    household_id = _linkable_household(engine, user)
    with repository(engine, household_id) as repo:
        item_ids = repo.plaid_item_ids()
    results = [refresh_item(engine, plaid_item_id, client) for plaid_item_id in item_ids]
    return {
        "household_id": household_id,
        "items_synced": len(results),
        "added": sum(r.sync.added for r in results),
        "modified": sum(r.sync.modified for r in results),
        "removed": sum(r.sync.removed for r in results),
        # The account-state half — the balances and card terms `build_linked_history` reads. Without
        # these the linked household stays undecidable, which is the gap this route now closes.
        "accounts": sum(r.accounts.accounts for r in results),
        "liabilities": sum(r.accounts.liabilities for r in results),
    }

"""POST /plaid/link/exchange — a public_token becomes a stored Item.

This is this backend's **first write path from the outside** (`POST /assistant/message` writes
nothing). It carries `Depends(require_api_key)` like every other route, so only the trusted internal
caller reaches it — there is no end-user identity yet (Clerk lands with the first real token).

**On `household_id`.** The plan says it is "fixture-supplied by the internal caller ... never read
from the request body" ([5.3]). There is no owner column and no `users` table, so an item is pinned
to a hand-picked household as a fixture, not a design. Here that fixture *is* the
API-key-authenticated caller's choice: the same trust model every other route runs under (the
shared key lets any
caller name any household — `tests/test_idor.py` is explicit that the mechanism is real and the
identity is not). The row is still written through `repository()`, so RLS `WITH CHECK` binds it to
the named household and a mismatch cannot smuggle a row elsewhere. A real end-user flow would derive
`household_id` from the authenticated session instead of trusting the caller — that is the deferral,
not a hole this rung opened.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.engine import Engine

from backend.auth import require_api_key
from backend.db.repository import repository
from backend.plaid.deps import get_engine, get_plaid_client

router = APIRouter(prefix="/plaid", tags=["plaid"])


class LinkExchangeRequest(BaseModel):
    public_token: str
    household_id: str
    institution_id: str | None = None


class LinkExchangeResponse(BaseModel):
    item_id: str
    household_id: str


@router.post("/link/exchange", dependencies=[Depends(require_api_key)])
def link_exchange(
    body: LinkExchangeRequest,
    engine: Annotated[Engine, Depends(get_engine)],
    client: Annotated[Any, Depends(get_plaid_client)],
) -> LinkExchangeResponse:
    from plaid.model.item_public_token_exchange_request import (
        ItemPublicTokenExchangeRequest,
    )

    exchange = client.item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=body.public_token)
    )
    with repository(engine, body.household_id) as repo:
        repo.add_plaid_item(
            item_id=f"pi-{exchange.item_id}",
            plaid_item_id=exchange.item_id,
            access_token=exchange.access_token,
            institution_id=body.institution_id,
        )
    return LinkExchangeResponse(item_id=exchange.item_id, household_id=body.household_id)

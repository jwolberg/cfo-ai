"""POST /plaid/link/exchange — a public_token becomes a stored Item, pinned to *your own* household.

The direct fix to the premise that opened the identity rung: "Plaid won't work without a real authed
user." Before the cutover this route trusted a `household_id` in the request body under the shared
key — any caller could pin an item anywhere (`tests/test_idor.py`'s standing caveat). Now:

- there is **no `household_id` on the wire** to smuggle. The target is derived from the verified
  session's membership (identity rung, KTD-2/KTD-8);
- it **refuses an `is_demo` household** outright, so a real bank item can never attach to the public
  demo plane (KTD-10) — the two planes cannot cross;
- linking is an **owner** action, so the demo `viewer` cannot reach it even for its own demo
  household.

The customer Link UI and onboarding (signup → create household → link) are the *next* rung; this
rung fixes the identity coupling so that rung has a stable per-user id to build on. Until then a
user's
linkable household is the single non-demo household they own; zero or many is a `409`, the ambiguity
the deferred Link UI will resolve by passing an explicit target.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.db.repository import households_for_user, repository
from backend.identity.deps import User, current_user
from backend.plaid.deps import get_engine, get_plaid_client

router = APIRouter(prefix="/plaid", tags=["plaid"])


class LinkTokenResponse(BaseModel):
    link_token: str
    expiration: str


@router.post("/link/token")
def link_token(
    request: Request,
    user: Annotated[User, Depends(current_user)],
    client: Annotated[Any, Depends(get_plaid_client)],
) -> LinkTokenResponse:
    """Mint a short-lived Plaid `link_token` for the signed-in user — the thing a client-side Link
    SDK needs to open. The missing first step of the loop (there was only `/link/exchange`, which
    assumed a `public_token` a UI produced).

    The token is keyed to `client_user_id = user.id` (our stable per-user id, ticket 0046), not a
    household — a household is chosen only at `/link/exchange`, from the session, so nothing here
    can point a link at someone else's data. `products=[transactions]` matches what the sync reads
    (`backend/plaid/sync.py`); Sandbox needs no more. Any signed-in user may mint one; the exchange
    is where ownership and the demo-plane refusal are enforced.
    """
    from plaid.model.country_code import CountryCode
    from plaid.model.link_token_create_request import LinkTokenCreateRequest
    from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
    from plaid.model.products import Products

    resp = client.link_token_create(
        LinkTokenCreateRequest(
            user=LinkTokenCreateRequestUser(client_user_id=user.id),
            client_name="cfo-ai",
            products=[Products("transactions")],
            country_codes=[CountryCode("US")],
            language="en",
        )
    )
    return LinkTokenResponse(link_token=resp.link_token, expiration=str(resp.expiration))


class LinkExchangeRequest(BaseModel):
    # No `household_id`: a user links to *their own* household, derived from the session. There is
    # nothing here to point at someone else's data.
    public_token: str
    institution_id: str | None = None


class LinkExchangeResponse(BaseModel):
    item_id: str
    household_id: str


def _linkable_household(engine: Engine, user: User) -> str:
    """The single non-demo household this user owns, or a `4xx` explaining why not.

    Resolves the caller's memberships (the SECURITY DEFINER lookup), drops `is_demo` households — a
    real item must never land on the demo plane (KTD-10) — and requires exactly one remaining. Zero
    means there is nowhere real to link yet (onboarding is the next rung); more than one is the
    ambiguity the deferred Link UI resolves by naming the target. Ownership is then confirmed under
    the scope, so a `viewer` cannot link even to a household they can read.
    """
    with engine.connect() as conn:
        allowed = households_for_user(conn, user.id)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No household to link a bank to yet.",
            )
        rows = (
            conn.execute(
                text("SELECT id, is_demo FROM households WHERE id = ANY(:ids)"),
                {"ids": allowed},
            )
            .mappings()
            .all()
        )

    non_demo = [r["id"] for r in rows if not r["is_demo"]]
    if not non_demo:
        # Every household this user belongs to is a demo one — a real bank cannot attach there.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A real bank cannot be linked to a demo household.",
        )
    if len(non_demo) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ambiguous target household; the Link flow will name it.",
        )

    household_id = non_demo[0]
    with repository(engine, household_id) as repo:
        if repo.member_role(user.id) != "owner":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Linking a bank requires an owner of the household.",
            )
    return household_id


@router.post("/link/exchange")
def link_exchange(
    body: LinkExchangeRequest,
    request: Request,
    user: Annotated[User, Depends(current_user)],
    engine: Annotated[Engine, Depends(get_engine)],
    client: Annotated[Any, Depends(get_plaid_client)],
) -> LinkExchangeResponse:
    from plaid.model.item_public_token_exchange_request import (
        ItemPublicTokenExchangeRequest,
    )

    household_id = _linkable_household(engine, user)

    exchange = client.item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=body.public_token)
    )
    with repository(engine, household_id) as repo:
        repo.add_plaid_item(
            item_id=f"pi-{exchange.item_id}",
            plaid_item_id=exchange.item_id,
            access_token=exchange.access_token,
            institution_id=body.institution_id,
        )
    return LinkExchangeResponse(item_id=exchange.item_id, household_id=household_id)

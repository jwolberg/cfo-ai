"""`current_user` and `authorize_household` — the two dependencies every user route will hang on.

Ticket 0047 (U2), KTD-2/KTD-5/KTD-9. **This file names no vendor.** It consumes an opaque verifier
(`stytch.verify` in production, a stub in tests) and the membership graph, and produces a verified
`User` and a household-scoped `Repository`. Swapping identity providers swaps `stytch.py`, not this.

The shape, and why:

- `current_user` verifies the bearer token and **JIT-provisions** a `users` row on an unknown
  `stytch_user_id` (KTD-5). A bad token is refused *before* any provisioning — `stytch.verify` does
  not touch the database, so an attacker cannot make us write a row with an unverified identity.
- `authorize_household` resolves the caller's memberships through the SECURITY DEFINER lookup and
  refuses (`403`) a household the caller does not belong to, before yielding a scoped repository
  (KTD-2). This is the single choke point a future step-up factor gates on (KTD-9) — designed here,
  built at money-on.
- `authorize_household_owner` is the same seam plus a role check: **writes require `owner`**
  (KTD-10), so the public demo `viewer` reads every demo household and writes none. The role is read
  from `household_members` *inside the scope*, where RLS makes exactly the caller's own row visible.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Path, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.db.repository import (
    Repository,
    add_user,
    get_user_by_stytch_id,
    households_for_user,
    repository,
)
from backend.identity import stytch

# A callable: session_token -> (stytch_user_id, claims). The seam that keeps this file vendor-free.
Verifier = Callable[[str], tuple[str, dict]]

# auto_error=False so a missing/!Bearer Authorization header reaches our handler and gets a uniform
# 401, rather than FastAPI's default 403-on-absence that tells a caller which failure they hit.
_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class User:
    """A verified user, resolved to our stable id. `id` is what the deferred Link rung passes to
    Plaid as `client_user_id`; `stytch_user_id` stays behind the edge adapter."""

    id: str
    stytch_user_id: str
    email: str | None


def get_verifier() -> Verifier:
    """The token verifier. Overridden in tests (FastAPI `dependency_overrides`) with a stub, so the
    dependency logic is exercised without a real Stytch project or network."""
    return stytch.verify


def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    verifier: Annotated[Verifier, Depends(get_verifier)],
) -> User:
    """Verify the bearer session and resolve it to our `User`, JIT-provisioning on first sight.

    Order matters: verification happens first and touches no database, so an invalid session is a
    `401` before any row is written. Only a *verified* `stytch_user_id` reaches provisioning.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer session token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        stytch_user_id, claims = verifier(credentials.credentials)
    except stytch.StytchVerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    engine = request.app.state.db
    with engine.connect() as conn, conn.begin():
        user = get_user_by_stytch_id(conn, stytch_user_id)
        if user is None:
            # ON CONFLICT DO NOTHING inside `add_user` makes this safe under the concurrent
            # first-login race — the second racer's insert is a no-op and the re-read below wins.
            add_user(
                conn,
                user_id=f"user_{uuid.uuid4().hex}",
                stytch_user_id=stytch_user_id,
                email=_claim_email(claims),
            )
            user = get_user_by_stytch_id(conn, stytch_user_id)

    assert user is not None  # just provisioned or already present
    return User(id=user["id"], stytch_user_id=user["stytch_user_id"], email=user["email"])


def _claim_email(claims: dict) -> str | None:
    """Best-effort email from the verified claims. Stytch carries it in the session object rather
    than a top-level claim; a missing email is a NULL, not a fabricated one (confirmed in U7)."""
    if isinstance(claims.get("email"), str):
        return claims["email"]
    session = claims.get("https://stytch.com/session")
    if isinstance(session, dict) and isinstance(session.get("email"), str):
        return session["email"]
    return None


def _authorize(
    request: Request, user: User, household_id: str, *, require_owner: bool
) -> Iterator[Repository]:
    """Membership-gate a household and yield a scoped repository, optionally requiring `owner`.

    The membership check runs on an unscoped connection through `households_for_user` (the SECURITY
    DEFINER lookup) *before* a scope is set — the requested id must be in the caller's set or the
    route refuses. Only then is the scoped repository opened. (This opens a second short-lived
    connection for the check; the single-connection optimisation is a logged follow-up, not a
    correctness issue — RLS still binds the scoped work.)
    """
    engine = request.app.state.db
    with engine.connect() as conn:
        allowed = households_for_user(conn, user.id)
    if household_id not in allowed:
        # Deliberately indistinguishable from "no such household": a valid session naming a
        # household it does not belong to learns nothing about whether it exists (KTD-2).
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your household.")

    with repository(engine, household_id) as repo:
        if require_owner and repo.member_role(user.id) != "owner":
            # The public demo `viewer` lands here on any write: it can read the household but not
            # change it (KTD-10). A structural read-only, not a read-only-by-convention.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This action requires an owner of the household.",
            )
        yield repo


def authorize_household(
    request: Request,
    user: Annotated[User, Depends(current_user)],
    household_id: Annotated[str, Path()],
) -> Iterator[Repository]:
    """A verified member's scoped repository for `household_id`, else `403`. Reads allowed for any
    member role."""
    yield from _authorize(request, user, household_id, require_owner=False)


def authorize_household_owner(
    request: Request,
    user: Annotated[User, Depends(current_user)],
    household_id: Annotated[str, Path()],
) -> Iterator[Repository]:
    """A verified **owner**'s scoped repository for `household_id`, else `403`. The write seam —
    `PATCH /policy`, `POST /attest`, link-exchange (KTD-10)."""
    yield from _authorize(request, user, household_id, require_owner=True)


def households_for(request: Request, user: User) -> list[str]:
    """The caller's household ids — for `GET /households`, which has no path id to authorize against
    and instead scopes its listing to membership directly (U3)."""
    with request.app.state.db.connect() as conn:
        return households_for_user(conn, user.id)


__all__ = [
    "User",
    "Verifier",
    "authorize_household",
    "authorize_household_owner",
    "current_user",
    "get_verifier",
    "households_for",
]

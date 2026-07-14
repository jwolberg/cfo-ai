"""The API key gate.

This is a lock on a door, not an identity system. There is one demo household, its data is
synthetic, and no request can move real money — so the key's job is to deter opportunistic
traffic (crawlers, scanners, someone who found the bare Cloud Run URL) rather than to
authenticate a person.

## The key is recoverable from the client, and that is the accepted trade

The same key ships inside the Expo bundle (`mobile/src/api/client.ts`), so anyone who
inspects the app can read it. That is not an oversight and it is not fixable at this tier:
a public client cannot hold a secret. What bounds the blast radius is that the data is
synthetic and read-only, and — for the one endpoint that costs money per call — the rate cap
on `POST /assistant/message` (U4), which is what actually caps Anthropic spend when the key
leaks. See the plan's Key Technical Decisions.

The key is never committed. It comes from the environment, and in production that
environment variable is a Secret Manager reference (`gcloud run deploy --set-secrets`), not
a literal: a plain env var is visible in `gcloud run services describe` and in the
deployment history, a secret reference is not.
"""

from __future__ import annotations

import os
import secrets

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

API_KEY_ENV = "RESFI_API_KEY"
API_KEY_HEADER = "X-API-Key"

# auto_error=False so a missing header reaches our own handler and gets the same response as
# a wrong one. FastAPI's default would 403 on absence and let us 401 on mismatch, which tells
# an unauthenticated caller which of the two they got — a small thing, and free to not do.
_scheme = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)


def expected_key() -> str:
    """The configured key, or a startup-time failure.

    Called at startup (not lazily on the first request) so a service deployed without its
    secret refuses to come up, rather than coming up and 401-ing every request while looking
    perfectly healthy to Cloud Run.
    """
    key = os.environ.get(API_KEY_ENV, "")
    if not key:
        raise RuntimeError(
            f"{API_KEY_ENV} is not set. The service will not start without an API key — "
            "an unauthenticated public endpoint is not a default worth having."
        )
    return key


async def require_api_key(provided: str | None = Security(_scheme)) -> None:
    """Gate a route. `Depends(require_api_key)` on the router, `/healthz` excepted."""
    expected = expected_key()

    # compare_digest rather than `==`: constant-time, and free. A timing oracle on this key
    # is not a realistic threat, but neither is writing the comparison the careless way.
    if provided is None or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key.",
            headers={"WWW-Authenticate": API_KEY_HEADER},
        )

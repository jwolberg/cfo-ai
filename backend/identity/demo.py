"""The public demo's durable session — a read-only viewer token the web app fetches, not baked.

Ticket 0057. The web bundle used to carry a static `EXPO_PUBLIC_DEMO_SESSION` baked at build time,
but a Stytch `session_jwt` lives only ~5 minutes and nothing refreshes it, so a baked token 401s a
few minutes after deploy (the "+ refresh" half of KTD-4 was never built). This mints one **server
side, on demand**, for a single pre-provisioned **viewer** identity, and caches it so traffic does
not hammer Stytch:

- It only ever mints the demo *viewer* — a `viewer` is refused every owner-gated write
  (`identity/deps.py:_authorize`), so a public, unauthenticated `POST /demo/session` handing this
  token to anyone is safe by construction: the worst a caller can do is read demo-plane households.
- The token is cached process-wide and re-minted only when it is within `_REMINT_MARGIN` of expiry,
  so N concurrent visitors cost ~one Stytch call per 5-minute window, not one per request.

The demo identity is a real Stytch user (so its tokens carry a real, verifiable signature and a real
`sub`) whose `stytch_user_id` the seeder/import reconciles onto the `users.stytch_user_id` of the
demo viewer row — see `scripts/seed_demo_household.py`. Configured by env:
`DEMO_STYTCH_EMAIL` + `DEMO_STYTCH_PASSWORD` (the password is a Secret Manager entry in prod).
"""

from __future__ import annotations

import datetime as dt
import os
import threading
from dataclasses import dataclass

# A Stytch session_jwt lives ~5 minutes; re-mint once it is within this margin of expiry so a
# handed-out token always has comfortably more than one request's worth of life left.
_JWT_TTL = dt.timedelta(minutes=5)
_REMINT_MARGIN = dt.timedelta(minutes=1)


class DemoSessionNotConfigured(RuntimeError):
    """Raised when `DEMO_STYTCH_EMAIL` / `DEMO_STYTCH_PASSWORD` are absent — the demo session cannot
    be minted without the identity it authenticates."""


@dataclass
class _Cached:
    session_jwt: str
    expires_at: dt.datetime


_cache: _Cached | None = None
_lock = threading.Lock()


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc)


def _mint(email: str, password: str) -> str:
    """Authenticate the demo Stytch user and return a fresh `session_jwt`. The one place this module
    talks to Stytch's server API (mirrors `tests/test_identity_sandbox.py:_mint_session`)."""
    from stytch import Client

    project_id = os.environ["STYTCH_PROJECT_ID"]
    client = Client(
        project_id=project_id,
        secret=os.environ["STYTCH_SECRET"],
        environment="test" if project_id.startswith("project-test-") else "live",
        suppress_warnings=True,
    )
    try:
        resp = client.passwords.authenticate(
            email=email, password=password, session_duration_minutes=60
        )
    except Exception:  # noqa: BLE001 — first run: the demo user doesn't exist yet, create it
        resp = client.passwords.create(email=email, password=password, session_duration_minutes=60)
    return resp.session_jwt


def demo_session_jwt() -> str:
    """A currently-valid demo viewer `session_jwt`, cached and re-minted before it expires.

    Raises `DemoSessionNotConfigured` if the demo identity env is absent — the route turns that into
    a 503 rather than a 500, since it is a deploy-config gap, not a request fault.
    """
    global _cache
    email = os.environ.get("DEMO_STYTCH_EMAIL", "").strip()
    password = os.environ.get("DEMO_STYTCH_PASSWORD", "")
    if not email or not password:
        raise DemoSessionNotConfigured(
            "DEMO_STYTCH_EMAIL and DEMO_STYTCH_PASSWORD must be set to mint the demo session."
        )

    now = _now()
    if _cache is not None and _cache.expires_at - now > _REMINT_MARGIN:
        return _cache.session_jwt

    with _lock:
        # Re-check under the lock: another thread may have just refreshed it.
        if _cache is not None and _cache.expires_at - _now() > _REMINT_MARGIN:
            return _cache.session_jwt
        jwt = _mint(email, password)
        _cache = _Cached(session_jwt=jwt, expires_at=_now() + _JWT_TTL)
        return jwt


def _reset_cache() -> None:
    """Clear the cached token. For tests only."""
    global _cache
    with _lock:
        _cache = None

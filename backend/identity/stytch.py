"""Local, offline verification of a Stytch session token — the one file that names the vendor.

Ticket 0047, KTD-4. This mirrors `backend/plaid/webhook.py:verify_webhook` deliberately: a JWT is
verified against a signing key fetched by `kid` from the issuer's JWKS and cached, and rejected on a
bad signature, algorithm, or expiry — with **no per-request round-trip** to Stytch. A session token
is trusted on signature + expiry alone, which is what makes the hot path a local computation.

Two differences from the Plaid webhook, both from KTD-4:

1. **The `kid` cache evicts on a TTL** (not the never-evict Plaid pattern). A webhook JWT is
   single-use; a *session* key is verified repeatedly over a session's life, so a rotated or
   compromised signing key must stop being trusted within a bounded window, not just on a process
   restart. `_JWKS_TTL` is that window.
2. **Stytch signs with RS256** (Plaid webhooks are ES256 over P-256). The algorithm is pinned and a
   token presenting any other `alg` is refused — the classic alg-confusion downgrade.

**Provider-reality caveat (to confirm in U7).** The issuer/audience shape encoded here is Stytch's
documented session-JWT form (`iss = stytch.com/<project_id>`, `aud = [<project_id>]`, `sub =` the
Stytch user id). The plan's Deferred Notes budget for ≥1 vendor-reality correction against the real
sandbox; if Stytch's live tokens disagree, this file — and only this file — is where it is fixed.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import urllib.request
from dataclasses import dataclass

import jwt
from jwt.algorithms import RSAAlgorithm

# A session signing key is trusted for at most this long after it is fetched. A rotation or a
# compromise-driven revocation of a Stytch signing key stops being honoured within this window,
# not only on process restart. Short enough to bound a stale-trust window, long enough that the
# JWKS is not refetched on every request.
_JWKS_TTL = dt.timedelta(minutes=10)

# Stytch signs session JWTs with RS256. Pinned so an attacker cannot present an HS256/none token and
# have it accepted against a key of the wrong type — the alg-confusion downgrade.
_ALG = "RS256"


class StytchConfigError(RuntimeError):
    """Raised at startup / first use when the Stytch project is not configured."""


class StytchVerificationError(Exception):
    """The session token was absent, malformed, unsigned, expired, wrong-audience, or signed by a
    key this project does not vouch for. Every one of them means: do not trust this session."""


def stytch_project_id() -> str:
    pid = os.environ.get("STYTCH_PROJECT_ID", "").strip()
    if not pid:
        raise StytchConfigError(
            "STYTCH_PROJECT_ID is not set — the identity edge cannot verify a session without the "
            "project it belongs to. See the identity rung plan; sandbox creds gate the real path."
        )
    return pid


def _stytch_host() -> str:
    """The API host for this project. Stytch serves **Test** projects from `test.stytch.com` and
    **Live** from `api.stytch.com`; the project id prefix (`project-test-…` / `project-live-…`) is
    self-describing, so the host follows from it and needs no `STYTCH_ENV`.

    Vendor-reality correction (confirmed 2026-07-18 against a real test project, U7/0053): the plan
    and an earlier draft here derived the JWKS URL from `api.stytch.com` unconditionally, which 404s
    for a test project. This is the one file the plan said such a correction would land in.
    """
    return (
        "test.stytch.com" if stytch_project_id().startswith("project-test-") else "api.stytch.com"
    )


def _jwks_url() -> str:
    """Where this project's public signing keys live.

    Overridable via `STYTCH_JWKS_URL` (a proxy, or a pinned host in a test); otherwise the Stytch
    sessions JWKS endpoint for this project on its environment's host.
    """
    override = os.environ.get("STYTCH_JWKS_URL", "").strip()
    if override:
        return override
    return f"https://{_stytch_host()}/v1/sessions/jwks/{stytch_project_id()}"


@dataclass
class _CachedKey:
    key: object
    fetched_at: dt.datetime


# kid -> (parsed RSA public key, when fetched). Guarded by a lock because FastAPI serves requests
# concurrently and two first-hits on a cold cache must not both race a fetch into it.
_key_cache: dict[str, _CachedKey] = {}
_cache_lock = threading.Lock()


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc)


def _fetch_jwks() -> dict:
    with urllib.request.urlopen(_jwks_url(), timeout=5) as resp:  # noqa: S310 — https URL from config
        return json.loads(resp.read())


def _signing_key_for(kid: str) -> object:
    """The RSA public key for `kid`, from cache or a fresh JWKS fetch. TTL-evicting (KTD-4).

    A cache entry older than `_JWKS_TTL` is treated as absent, so a rotated key is picked up within
    the window and a revoked one stops being trusted. A `kid` miss (a new key) also refetches.
    """
    cached = _key_cache.get(kid)
    if cached is not None and _now() - cached.fetched_at <= _JWKS_TTL:
        return cached.key

    with _cache_lock:
        # Re-check under the lock: another thread may have just refreshed it.
        cached = _key_cache.get(kid)
        if cached is not None and _now() - cached.fetched_at <= _JWKS_TTL:
            return cached.key

        jwks = _fetch_jwks()
        found: object | None = None
        for jwk in jwks.get("keys", []):
            if jwk.get("kid"):
                _key_cache[jwk["kid"]] = _CachedKey(
                    key=RSAAlgorithm.from_jwk(json.dumps(jwk)), fetched_at=_now()
                )
                if jwk["kid"] == kid:
                    found = _key_cache[kid].key
        if found is None:
            raise StytchVerificationError(
                f"no signing key with kid {kid!r} in the Stytch JWKS — unknown or rotated away"
            )
        return found


def verify(session_token: str) -> tuple[str, dict]:
    """Verify a Stytch session JWT locally and return `(stytch_user_id, claims)`, or raise.

    Nothing here touches the database — a bad token is refused before any user is provisioned or any
    row is read (the "rejected before any DB touch" property `current_user` relies on). The order is
    header → alg → kid → signature+claims, cheapest refusal first.
    """
    if not session_token:
        raise StytchVerificationError("no session token")

    try:
        header = jwt.get_unverified_header(session_token)
    except jwt.PyJWTError as exc:
        raise StytchVerificationError("the session token is not a JWT") from exc

    if header.get("alg") != _ALG:
        raise StytchVerificationError(
            f"unexpected JWT alg {header.get('alg')!r}; Stytch signs sessions with {_ALG}"
        )
    kid = header.get("kid")
    if not kid:
        raise StytchVerificationError("the JWT header carries no kid")

    project_id = stytch_project_id()
    key = _signing_key_for(kid)
    try:
        claims = jwt.decode(
            session_token,
            key=key,
            algorithms=[_ALG],
            audience=project_id,
            issuer=f"stytch.com/{project_id}",
            options={"require": ["iat", "exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise StytchVerificationError(f"the session token did not verify: {exc}") from exc

    stytch_user_id = claims.get("sub")
    if not stytch_user_id:
        raise StytchVerificationError("the verified token carries no subject (stytch user id)")
    return stytch_user_id, claims


def _reset_key_cache() -> None:
    """Clear the JWKS cache. For tests only — production relies on the TTL, not a manual flush."""
    with _cache_lock:
        _key_cache.clear()

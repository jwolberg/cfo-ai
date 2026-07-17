"""Verifying the OIDC token Cloud Tasks and Cloud Scheduler put on the worker's push target.

The sync worker and the nightly poll are internal endpoints that must be reachable by Google's
infrastructure and no one else. Cloud Tasks (and Cloud Scheduler) can mint an OIDC token for the
configured service account, scoped to the target URL as its audience; this verifies that token
against Google's public keys and checks both the audience and the email. Anything else — no token, a
token for a different audience, a different service account — is refused, and the route returns 401.

Network-dependent by nature (Google's certs), so it is injected as a FastAPI dependency and tests
substitute a stub. The no-token path short-circuits to `False` before any network call.
"""

from __future__ import annotations

import os


def verify_google_oidc(authorization: str | None) -> bool:
    """True only for a valid Google-issued OIDC token for our audience and service account."""
    if not authorization or not authorization.startswith("Bearer "):
        return False
    audience = os.environ.get("PLAID_SYNC_WORKER_URL")
    expected_sa = os.environ.get("PLAID_TASKS_SERVICE_ACCOUNT")
    if not audience or not expected_sa:
        return False

    token = authorization.split(" ", 1)[1]
    try:
        from google.auth.transport import requests as ga_requests
        from google.oauth2 import id_token

        claims = id_token.verify_oauth2_token(token, ga_requests.Request(), audience=audience)
    except Exception:
        # A bad signature, a wrong audience, an expired token, or an unreachable cert endpoint all
        # mean the same thing here: do not run the worker for this request.
        return False

    return bool(claims.get("email_verified")) and claims.get("email") == expected_sa

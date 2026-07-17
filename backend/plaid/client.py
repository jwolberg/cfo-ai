"""The Plaid API client, built from the environment, cached for the process.

Sandbox only for this rung. `plaid.Environment` in v39 is `Sandbox` or `Production` — there is no
`Development` any more — and the start guard (`backend/db/session.py`) refuses a non-sandbox boot
until KMS makes the access token ciphertext, so nothing here reaches Production yet.

The client is built **lazily**, on the first call that needs it, never at startup: Plaid is
additive, and a service with no Plaid credentials must still come up and serve everything else.
Contrast the Anthropic client, built in the lifespan because the explain path is not optional.
"""

from __future__ import annotations

import os

import plaid
from plaid.api import plaid_api

from backend.db.session import plaid_env


class PlaidNotConfigured(RuntimeError):
    """Raised when a Plaid call is attempted without the credentials or environment to make it."""


# `plaid_env()` returns lowercase; map to the SDK's environment hosts.
_ENVIRONMENTS = {
    "sandbox": plaid.Environment.Sandbox,
    "production": plaid.Environment.Production,
}

_client: plaid_api.PlaidApi | None = None


def _credentials() -> tuple[str, str]:
    client_id = os.environ.get("PLAID_CLIENT_ID")
    secret = os.environ.get("PLAID_SECRET")
    if not client_id or not secret:
        raise PlaidNotConfigured(
            "PLAID_CLIENT_ID and PLAID_SECRET must both be set to call Plaid. They are absent, so "
            "no Link exchange or webhook verification can run — see docs/tickets/0035."
        )
    return client_id, secret


def make_plaid_client() -> plaid_api.PlaidApi:
    """Build a fresh client from the environment. Prefer `plaid_client()` outside of tests."""
    env = plaid_env()
    host = _ENVIRONMENTS.get(env)
    if host is None:
        raise PlaidNotConfigured(
            f"PLAID_ENV={env!r} is not a Plaid environment (sandbox|production)."
        )
    client_id, secret = _credentials()
    configuration = plaid.Configuration(
        host=host, api_key={"clientId": client_id, "secret": secret}
    )
    return plaid_api.PlaidApi(plaid.ApiClient(configuration))


def plaid_client() -> plaid_api.PlaidApi:
    """The process-wide client, built on first use. Tests override the FastAPI dependency."""
    global _client
    if _client is None:
        _client = make_plaid_client()
    return _client

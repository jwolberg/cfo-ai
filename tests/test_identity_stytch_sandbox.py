"""The Stytch adapter, against a real Stytch project's JWKS — not a generated key.

Ticket 0047 (U2). The stubbed suite in `tests/test_identity_deps.py` proves the decode logic against
a keypair we minted; this file proves the one thing it cannot — that `stytch._signing_key_for`
actually fetches and parses **Stytch's own** JWKS for a configured project. It is the U2-level
sandbox smoke; the full session-verify-to-attest end-to-end gate is U7 (`test_identity_sandbox.py`).

**It requires a real Stytch project id** (`STYTCH_PROJECT_ID`, optionally `STYTCH_JWKS_URL`).
Without it, it **skips loudly** rather than reporting a green it did not earn — the same posture the
Plaid Sandbox gate and the database suites take, and the exact "built, tested, never exercised"
failure this rung exists to answer.

> Provenance: NOT yet run against a real Stytch project — no sandbox credentials here.
> U7 is the hard gate and must run green (with any vendor-reality correction folded into
> `backend/identity/stytch.py`) before the identity path is trusted.
"""

from __future__ import annotations

import os

import pytest

from backend.identity import stytch

_HAVE_PROJECT = bool(os.environ.get("STYTCH_PROJECT_ID"))
_SKIP = (
    "the Stytch adapter smoke needs STYTCH_PROJECT_ID (a real Stytch project).\n"
    "  This fetches Stytch's own JWKS — the real-vendor half the stubbed keypair suite cannot\n"
    "  reach. Set STYTCH_PROJECT_ID (Stytch test projects are free), then run it. A skipped gate\n"
    "  is an adapter that has never met the vendor it adapts."
)

requires_stytch = pytest.mark.skipif(not _HAVE_PROJECT, reason=_SKIP)


@requires_stytch
def test_the_project_jwks_is_reachable_and_parses() -> None:
    """A live JWKS fetch returns at least one usable signing key for the configured project. This is
    what `verify()` depends on before it can check a single real session."""
    stytch._reset_key_cache()
    jwks = stytch._fetch_jwks()
    keys = jwks.get("keys", [])
    assert keys, "the Stytch JWKS returned no keys"
    kid = keys[0]["kid"]
    key = stytch._signing_key_for(kid)
    assert key is not None

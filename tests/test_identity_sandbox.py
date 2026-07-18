"""The hard gate: the identity path, proven against a **real Stytch sandbox** — not a stub.

This is the reason to build the other units. Every other identity test fakes the verifier at the
seam (`tests/test_identity_deps.py`, `test_route_authz.py`, `test_policy_write.py`,
`test_attestation.py`); this one drives real Stytch sessions through the whole path:

    mint a real session for a sandbox user
      → current_user verifies it (real JWKS) and JIT-provisions our `users` row
      → authorize_household 403s a household the user is not a member of
      → fixture-membership reaches a seeded household
      → PATCH /policy writes an audited, append-only change (assert policy_events)
      → POST /attest clears the coverage money-gate in shadow
      → a second sandbox user proves cross-user isolation

**It requires live Stytch sandbox credentials** (`STYTCH_PROJECT_ID`, `STYTCH_SECRET`) + Postgres.
Without them it **skips loudly** rather than reporting a green it did not earn — the same posture
as the Plaid Sandbox gate (`tests/test_plaid_sandbox.py`) and the database suites, and the exact
"a mechanism built, tested, and never actually exercised" failure this repo keeps finding (tickets
0021, 0033, 0038; the sweep rung's U6).

> ⛔ Provenance: **NOT yet run against a real Stytch sandbox** — no Stytch credentials here. The
> plan budgets for ≥1 vendor-reality correction (the Plaid and sweep rungs each hit one); when this
> runs, any correction to the session-JWT shape lands in
> `backend/identity/stytch.py` — the one file that names the vendor — and the provenance above is
> updated to "green against real Stytch sandbox on <date>". Until then the identity path is proven
> only against a stubbed verifier, which this rung's own philosophy does not accept as evidence.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.db.session import stytch_env
from tests.conftest import _url as _db_url

# The real thing needs both halves of the Stytch project: the public id (to verify the JWKS) and
# the secret (to mint sessions). `STYTCH_ENV` must not be `live` — this mints throwaway sessions
# against a test project, exactly as the Plaid gate insists on `sandbox`.
_HAVE_CREDENTIALS = bool(os.environ.get("STYTCH_PROJECT_ID") and os.environ.get("STYTCH_SECRET"))
_SKIP = (
    "the Stytch identity gate needs STYTCH_PROJECT_ID, STYTCH_SECRET (a real Stytch **test**\n"
    "  project), and TEST_DATABASE_URL. This is a REAL sandbox run, not a stub — the one test\n"
    "  that proves the identity path against Stytch itself: real verification, JIT provisioning,\n"
    "  the membership 403, an audited policy write, the attestation money-gate, and cross-user\n"
    "  isolation. Set the credentials (Stytch test projects are free) and run it. A skipped hard\n"
    "  gate is an identity mechanism nobody has actually exercised."
)

requires_stytch_sandbox = pytest.mark.skipif(
    not (_HAVE_CREDENTIALS and stytch_env() != "live" and _db_url() is not None),
    reason=_SKIP,
)

pytestmark = requires_stytch_sandbox


def _mint_session(sandbox_user_email: str) -> str:
    """Mint a real Stytch **session JWT** for a sandbox user, creating the user if needed.

    Uses Stytch's server-side API with the project secret. Kept in one place so the vendor-reality
    correction the plan budgets for — if Stytch's create-user / session-mint shape differs from
    the documented one — lands here and in `backend/identity/stytch.py`, and nowhere else.

    Implemented against Stytch's documented Sessions API; the exact endpoint/params are confirmed
    when this first runs with real credentials (see the module provenance note).
    """
    import stytch  # noqa: F401 — the Stytch SDK; an ImportError here is a loud "install stytch"

    raise NotImplementedError(
        "wire _mint_session to Stytch's server API when running with real credentials — "
        "create/find the sandbox user and return a session_jwt. Left explicit so this gate cannot "
        "pass without actually minting a real session (the whole point of U7)."
    )


@pytest.fixture
def real_verifier_app(app_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app with the **real** Stytch verifier (no dependency override) — this is what makes the
    session verification real rather than stubbed."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    monkeypatch.setenv("DATABASE_URL", app_engine.url.render_as_string(hide_password=False))
    from backend.main import app

    with TestClient(app) as c:
        yield c


def test_a_real_session_verifies_and_jit_provisions(real_verifier_app: TestClient) -> None:
    token = _mint_session("gate-user-a@example.com")
    # A real session on GET /households: verified against Stytch's JWKS, provisioning a users row.
    resp = real_verifier_app.get("/households", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_a_real_non_member_session_is_refused(real_verifier_app: TestClient) -> None:
    token = _mint_session("gate-user-a@example.com")
    resp = real_verifier_app.get(
        "/households/hh_demo_biweekly/decisions", headers={"Authorization": f"Bearer {token}"}
    )
    # Not a member (no fixture membership) → the valid-but-non-member refusal, against a real token.
    assert resp.status_code == 403


def test_the_write_paths_are_audited_and_isolated(
    real_verifier_app: TestClient, app_engine: Engine
) -> None:
    """Fixture-member a real sandbox user into a seeded household, then drive the write paths and a
    second user to prove isolation. Filled in when run with credentials; the structure is fixed."""
    _ = app_engine
    with app_engine.begin() as conn:
        conn.execute(text("SELECT 1"))  # placeholder for the fixture-membership setup
    pytest.skip("body wired when run with real Stytch credentials — see module provenance")

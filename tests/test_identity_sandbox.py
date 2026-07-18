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

> ✅ Provenance: **green against a real Stytch test project on 2026-07-18.** One vendor-reality
> correction (the plan budgeted for ≥1, as the Plaid and sweep rungs each hit): the JWKS host is
> **environment-specific** — Stytch serves Test from `test.stytch.com`, not the `api.stytch.com` an
> earlier draft derived unconditionally (404s a test project). Fixed in
> `backend/identity/stytch.py` (`_stytch_host`), the one file that names the vendor. The session-JWT
> shape the adapter verifies (`iss = stytch.com/<pid>`, `aud = [<pid>]`, `sub` = the Stytch user id)
> was confirmed **correct** against a real minted token — no change there. Without credentials this
> still skips loudly (below), so CI keeps proving it can, not that it did.
"""

from __future__ import annotations

import os
import uuid
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


# A fixed strong password for the sandbox users this gate mints. Not a secret worth guarding (it
# authenticates only throwaway users in a Stytch test project), and constant so a second mint of the
# same user within a run authenticates rather than fails to re-create.
_SANDBOX_PASSWORD = "gate-correct-horse-battery-staple-9x!"

# A per-process tag, so each run mints **fresh** sandbox users (create always succeeds) while the
# same logical user keeps one email across this process's tests. Avoids colliding with users a
# previous run left at a different password. (Test-project users are throwaway; clean them from the
# Stytch dashboard periodically.)
_RUN = uuid.uuid4().hex[:12]


def _email(who: str) -> str:
    return f"gate-{who}-{_RUN}@example.com"


def _mint_session(email: str) -> tuple[str, str]:
    """Mint a real Stytch session JWT for a sandbox user; return `(session_jwt, stytch_user_id)`.

    Create-or-authenticate via the Passwords product, so a re-run of this gate authenticates the
    user it created last time rather than erroring. This is the one place that talks to Stytch's
    server API, and the one place a vendor-reality correction lands — as it did on the first real
    run (2026-07-18): the JWKS host is environment-specific (`test.stytch.com`), fixed in
    `backend/identity/stytch.py`. The session-JWT shape our adapter verifies (`iss`/`aud`/`sub`) was
    confirmed correct against a real minted token.
    """
    from stytch import Client

    client = Client(
        project_id=os.environ["STYTCH_PROJECT_ID"],
        secret=os.environ["STYTCH_SECRET"],
        environment="test",
        suppress_warnings=True,
    )
    try:
        resp = client.passwords.create(
            email=email, password=_SANDBOX_PASSWORD, session_duration_minutes=60
        )
    except Exception:  # noqa: BLE001 — an existing sandbox user; authenticate instead of create
        resp = client.passwords.authenticate(
            email=email, password=_SANDBOX_PASSWORD, session_duration_minutes=60
        )
    return resp.session_jwt, resp.user_id


@pytest.fixture
def real_verifier_app(app_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app with the **real** Stytch verifier (no dependency override) — this is what makes the
    session verification real rather than stubbed."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    monkeypatch.setenv("DATABASE_URL", app_engine.url.render_as_string(hide_password=False))
    from backend.main import app

    with TestClient(app) as c:
        yield c


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _provisioned_user_id(client: TestClient, token: str, stytch_user_id: str, db) -> str:
    """Drive one authed request so `current_user` JIT-provisions our `users` row; return our id.

    The request is `GET /households` (verifies the real session, provisions, returns the caller's —
    empty — memberships). Our `users.id` is minted at provisioning, so a membership can only be
    fixtured *after* this.
    """
    resp = client.get("/households", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    with db.begin():
        uid = db.execute(
            text("SELECT id FROM users WHERE stytch_user_id = :s"), {"s": stytch_user_id}
        ).scalar()
    assert uid, "a verified real session did not JIT-provision a users row"
    return uid


def _seed_household_for(db, household: str, user_id: str) -> None:
    """As the superuser: a household, an owner membership for `user_id`, a card, and an initial
    policy so the write paths have something to act on."""
    with db.begin():
        db.execute(
            text("INSERT INTO households (id, archetype) VALUES (:h, 'test')"), {"h": household}
        )
        db.execute(
            text(
                "INSERT INTO household_members (household_id, user_id, role)"
                " VALUES (:h, :u, 'owner')"
            ),
            {"h": household, "u": user_id},
        )
        db.execute(
            text(
                "INSERT INTO cards (id, household_id, apr, close_day_of_month, grace_days,"
                " statement_balance, statement_due_date, minimum_payment, unbilled_balance,"
                " next_close_date, behavior) VALUES (:cid, :h, '0.2399', 20, 21, '1000.00',"
                " '2026-02-10', '25.00', '0.00', '2026-01-20', 'revolver')"
            ),
            {"cid": f"card-{household}", "h": household},
        )
        db.execute(
            text(
                "INSERT INTO policy_events (id, household_id, buffer_floor, max_sweep,"
                " max_weekly_sweep, min_days_between_sweeps) VALUES"
                " (:pid, :h, '800.00', '1600.00', '3200.00', 7)"
            ),
            {"pid": f"pe-{household}-init", "h": household},
        )


_VALID_POLICY = {
    "buffer_floor": "500.00",
    "max_sweep": "1600.00",
    "max_weekly_sweep": "3200.00",
    "min_days_between_sweeps": 7,
    "blackout_dates": [],
}


def test_a_real_session_verifies_and_jit_provisions(real_verifier_app: TestClient, db) -> None:
    token, sid = _mint_session(_email("a"))
    # A real session on GET /households: verified against Stytch's JWKS, provisioning a users row.
    resp = real_verifier_app.get("/households", headers=_bearer(token))
    assert resp.status_code == 200
    with db.begin():
        n = db.execute(
            text("SELECT count(*) FROM users WHERE stytch_user_id = :s"), {"s": sid}
        ).scalar()
    assert n == 1, "a verified real session did not provision exactly one users row"


def test_a_real_non_member_session_is_refused(real_verifier_app: TestClient) -> None:
    token, _ = _mint_session(_email("a"))
    resp = real_verifier_app.get("/households/hh_demo_biweekly/decisions", headers=_bearer(token))
    # Not a member (no fixture membership) → the valid-but-non-member refusal, against a real token.
    assert resp.status_code == 403


def test_the_write_paths_are_audited(real_verifier_app: TestClient, db) -> None:
    """A real member drives the two writes; both land and are attributed to the real user."""
    token, sid = _mint_session(_email("a"))
    uid = _provisioned_user_id(real_verifier_app, token, sid, db)
    _seed_household_for(db, "gate_hh_a", uid)

    patched = real_verifier_app.patch(
        "/households/gate_hh_a/policy", headers=_bearer(token), json=_VALID_POLICY
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["buffer_floor"] == "500.00"

    attested = real_verifier_app.post("/households/gate_hh_a/attest", headers=_bearer(token))
    assert attested.status_code == 200, attested.text
    assert attested.json()["attested"] is True

    # Both writes are audited and attributed to the real user id.
    with db.begin():
        policy_by = db.execute(
            text(
                "SELECT changed_by FROM policy_events WHERE household_id = 'gate_hh_a'"
                " ORDER BY seq DESC LIMIT 1"
            )
        ).scalar()
        attest_by = db.execute(
            text(
                "SELECT attested_by FROM card_attestations WHERE household_id = 'gate_hh_a'"
                " ORDER BY seq DESC LIMIT 1"
            )
        ).scalar()
    assert policy_by == uid, "the policy write was not attributed to the real user"
    assert attest_by == uid, "the attestation was not attributed to the real user"


def test_two_real_users_are_mutually_invisible(real_verifier_app: TestClient, db) -> None:
    """Cross-user isolation, against two real Stytch sessions — the property the whole rung exists
    for, proven at the HTTP layer with the real verifier."""
    token_a, sid_a = _mint_session(_email("a"))
    token_b, sid_b = _mint_session(_email("b"))
    uid_a = _provisioned_user_id(real_verifier_app, token_a, sid_a, db)
    uid_b = _provisioned_user_id(real_verifier_app, token_b, sid_b, db)
    _seed_household_for(db, "gate_hh_a", uid_a)
    _seed_household_for(db, "gate_hh_b", uid_b)

    # Each sees only their own household in the list.
    a_list = {
        h["id"]
        for h in real_verifier_app.get("/households", headers=_bearer(token_a)).json()["households"]
    }
    b_list = {
        h["id"]
        for h in real_verifier_app.get("/households", headers=_bearer(token_b)).json()["households"]
    }
    assert a_list == {"gate_hh_a"}
    assert b_list == {"gate_hh_b"}

    # And neither can reach the other's household directly.
    assert (
        real_verifier_app.get(
            "/households/gate_hh_b/decisions", headers=_bearer(token_a)
        ).status_code
        == 403
    )
    assert (
        real_verifier_app.get(
            "/households/gate_hh_a/decisions", headers=_bearer(token_b)
        ).status_code
        == 403
    )

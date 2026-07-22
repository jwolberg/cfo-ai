"""The demo plane cannot cross into the real one (ticket 0058).

`authorize_household_owner` refuses the demo `viewer` every household-scoped write, and
`tests/test_route_authz.py` holds that. But role is **per household**, so the routes with no
`household_id` to authorize against had no role to check:

- `POST /households` — creates the very household a role would be scoped to. Measured in production
  on 2026-07-21 returning **201** to the public demo session, which then made that session the
  household's *owner* and re-opened `PATCH /policy`, `POST /attest` and `POST /plaid/link/exchange`
  on it.
- `POST /plaid/link/token` and `POST /plaid/link/exchange` — keyed to the user, not a household.

`current_real_user` closes them by reading `users.is_demo`. The tests that matter are that each
route refuses a demo identity, that a real identity is unaffected, and — the acceptance criterion —
that the refusal follows the **row**, so a demo identity created later is refused with no code
change and no env var.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.identity import stytch
from backend.identity.deps import get_verifier
from tests.conftest import requires_db

pytestmark = requires_db

REAL_TOKEN, REAL_STYTCH_ID = "real-user-token", "stytch-real-user"
DEMO_TOKEN, DEMO_STYTCH_ID = "demo-viewer-token", "stytch-demo-plane-user"


def _verifier():
    def verify(token: str):
        if token == REAL_TOKEN:
            return REAL_STYTCH_ID, {}
        if token == DEMO_TOKEN:
            return DEMO_STYTCH_ID, {}
        raise stytch.StytchVerificationError("stub: unknown token")

    return verify


@pytest.fixture(autouse=True)
def secrets(monkeypatch: pytest.MonkeyPatch, app_engine: Engine) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    monkeypatch.setenv("DATABASE_URL", app_engine.url.render_as_string(hide_password=False))


@pytest.fixture
def client(db) -> Iterator[TestClient]:
    from backend.main import app

    app.dependency_overrides[get_verifier] = _verifier
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def demo_caller(client: TestClient, app_engine: Engine) -> None:
    """A JIT-provisioned user flipped onto the demo plane.

    Deliberately *not* the seeded demo viewer and *not* `DEMO_STYTCH_EMAIL`: the gate must follow
    the row, so an identity that did not exist when the code was written is refused anyway.
    Provisioning happens through a real request (`GET /households`) — the row shape production has.
    """
    assert client.get("/households", headers=_auth(DEMO_TOKEN)).status_code == 200
    with app_engine.begin() as conn:
        updated = conn.execute(
            text("UPDATE users SET is_demo = true WHERE stytch_user_id = :s"),
            {"s": DEMO_STYTCH_ID},
        ).rowcount
    assert updated == 1


class TestTheDemoPlaneCannotCreateAHousehold:
    def test_a_demo_identity_is_refused(self, client: TestClient, demo_caller: None) -> None:
        resp = client.post("/households", headers=_auth(DEMO_TOKEN))
        assert resp.status_code == 403
        assert resp.json()["detail"] == "The demo session is read-only."

    def test_nothing_was_created(self, client: TestClient, demo_caller: None, app_engine) -> None:
        """The refusal is before the write, not a rollback after it — a 403 that still left a
        household behind would leave the public picker showing it (the production symptom)."""
        client.post("/households", headers=_auth(DEMO_TOKEN))
        with app_engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM households WHERE NOT is_demo")).scalar()
                == 0
            )

    def test_a_real_identity_is_unaffected(self, client: TestClient) -> None:
        resp = client.post("/households", headers=_auth(REAL_TOKEN))
        assert resp.status_code == 201
        assert resp.json()["created"] is True

    def test_a_new_user_is_not_a_demo_user_by_omission(
        self, client: TestClient, app_engine: Engine
    ) -> None:
        """`is_demo` defaults false: JIT provisioning must not strand a real user on the demo
        plane."""
        client.get("/households", headers=_auth(REAL_TOKEN))
        with app_engine.connect() as conn:
            flag = conn.execute(
                text("SELECT is_demo FROM users WHERE stytch_user_id = :s"), {"s": REAL_STYTCH_ID}
            ).scalar()
        assert flag is False


class TestTheDemoPlaneCannotLinkABank:
    """Both Link routes are user-keyed, so neither had a role to check. Refused before any Plaid
    call — a demo caller should not be able to burn quota either."""

    def test_link_token_is_refused(self, client: TestClient, demo_caller: None) -> None:
        assert client.post("/plaid/link/token", headers=_auth(DEMO_TOKEN)).status_code == 403

    def test_link_exchange_is_refused(self, client: TestClient, demo_caller: None) -> None:
        resp = client.post(
            "/plaid/link/exchange",
            headers=_auth(DEMO_TOKEN),
            json={"public_token": "public-sandbox-irrelevant"},
        )
        assert resp.status_code == 403


class TestTheDemoViewerReads:
    """The gate must not cost the demo its whole reason for existing: reading is still fine."""

    def test_a_demo_identity_can_still_list_households(
        self, client: TestClient, demo_caller: None
    ) -> None:
        assert client.get("/households", headers=_auth(DEMO_TOKEN)).status_code == 200

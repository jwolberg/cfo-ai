"""POST /households — the onboarding write path (create a real household, become its owner).

The identity rung deferred "signup → create household → link" (Decision 3); this is the create-
household half, the precondition a real bank link needs. The claims worth holding:

- a session is required (`401` without one);
- a first call **creates a non-demo household** and makes the caller its **owner**, through the
  repository so RLS `WITH CHECK` binds the membership;
- it is **idempotent for the single-household case** the Link flow assumes — a second call returns
  the same household with `created: false`, not a second one;
- already belonging to more than one real household is the ambiguity the Link flow refuses (`409`).

The verifier is stubbed so no real Stytch project is needed (as in `test_link.py`).
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

TOKEN = "onboarder-session-token"
STYTCH_ID = "stytch-onboarder"


def _verifier():
    def verify(token: str):
        if token == TOKEN:
            return STYTCH_ID, {}
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


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_a_session_is_required(client: TestClient) -> None:
    assert client.post("/households").status_code == 401


def test_a_first_call_creates_a_real_household_owned_by_the_caller(
    client: TestClient, db, app_engine: Engine
) -> None:
    resp = client.post("/households", headers=_auth())
    assert resp.status_code == 201
    body = resp.json()
    assert body["created"] is True
    hid = body["household_id"]

    with db.begin():
        row = db.execute(
            text("SELECT archetype, is_demo FROM households WHERE id = :h"), {"h": hid}
        ).one()
        # A real household: no archetype, not on the demo plane.
        assert row.archetype is None and row.is_demo is False
        member = db.execute(
            text(
                "SELECT u.stytch_user_id AS sid, m.role AS role"
                " FROM household_members m JOIN users u ON u.id = m.user_id"
                " WHERE m.household_id = :h"
            ),
            {"h": hid},
        ).one()
    assert member.sid == STYTCH_ID
    assert member.role == "owner"


def test_it_is_idempotent_for_a_single_household(client: TestClient) -> None:
    first = client.post("/households", headers=_auth())
    assert first.status_code == 201 and first.json()["created"] is True
    hid = first.json()["household_id"]

    second = client.post("/households", headers=_auth())
    # The single-household case the Link flow assumes: the same household, not a second one.
    assert second.status_code == 200
    assert second.json() == {"household_id": hid, "created": False}


def test_belonging_to_two_real_households_is_a_409(
    client: TestClient, db, app_engine: Engine
) -> None:
    # First provision the user by creating one household through the route...
    hid1 = client.post("/households", headers=_auth()).json()["household_id"]
    # ...then arrange a second real membership out of band (the state the Link flow refuses).
    with db.begin():
        uid = db.execute(
            text("SELECT id FROM users WHERE stytch_user_id = :s"), {"s": STYTCH_ID}
        ).scalar_one()
        db.execute(
            text("INSERT INTO households (id, archetype, is_demo) VALUES ('hh_other', NULL, false)")
        )
        db.execute(
            text(
                "INSERT INTO household_members (household_id, user_id, role)"
                " VALUES ('hh_other', :u, 'owner')"
            ),
            {"u": uid},
        )

    resp = client.post("/households", headers=_auth())
    assert resp.status_code == 409
    assert hid1 != "hh_other"  # sanity: two distinct real households now exist

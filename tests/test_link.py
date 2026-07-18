"""POST /plaid/link/exchange — the first write path from outside, after the identity cutover.

Ticket 0048. This route no longer trusts a `household_id` in the body: it requires a verified
session and links to the caller's *own* household, from membership. The claims worth holding:

- a session is required (`401` without one);
- a successful exchange writes an item **to the caller's single non-demo owned household**, through
  the repository so RLS `WITH CHECK` binds it;
- it **refuses an `is_demo` household** (`403`), so a real bank item never lands on the demo plane;
- zero or many candidate households is a `409` — the ambiguity the deferred Link UI resolves.

The Plaid exchange is faked so no token leaves the test; the verifier is stubbed so no real Stytch
project is needed (U7 is the real-Stytch gate).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.identity import stytch
from backend.identity.deps import get_verifier
from backend.plaid.deps import get_plaid_client
from tests.conftest import requires_db

pytestmark = requires_db

TOKEN = "linker-session-token"
STYTCH_ID = "stytch-linker"


class _Exchange:
    def __init__(self, access_token: str, item_id: str) -> None:
        self.access_token = access_token
        self.item_id = item_id


class FakeLinkClient:
    def __init__(self) -> None:
        self.exchanged: list[str] = []

    def item_public_token_exchange(self, request):
        self.exchanged.append(request.public_token)
        return _Exchange(access_token="access-sandbox-tok", item_id="item-linked")


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
def fake_client() -> FakeLinkClient:
    return FakeLinkClient()


@pytest.fixture
def client(db, fake_client: FakeLinkClient) -> Iterator[TestClient]:
    from backend.main import app

    app.dependency_overrides[get_plaid_client] = lambda: fake_client
    app.dependency_overrides[get_verifier] = _verifier
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _member(db, *, household: str, is_demo: bool = False, role: str = "owner") -> None:
    db.execute(
        text("INSERT INTO households (id, archetype, is_demo) VALUES (:h, 'test', :d)"),
        {"h": household, "d": is_demo},
    )
    db.execute(
        text(
            "INSERT INTO users (id, stytch_user_id) VALUES ('u-linker', :s) ON CONFLICT DO NOTHING"
        ),
        {"s": STYTCH_ID},
    )
    db.execute(
        text(
            "INSERT INTO household_members (household_id, user_id, role)"
            " VALUES (:h, 'u-linker', :r)"
        ),
        {"h": household, "r": role},
    )


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_a_session_is_required(client: TestClient) -> None:
    response = client.post("/plaid/link/exchange", json={"public_token": "public-x"})
    assert response.status_code == 401


def test_a_successful_exchange_writes_an_item_to_the_callers_household(
    client: TestClient, fake_client: FakeLinkClient, db
) -> None:
    with db.begin():
        _member(db, household="h1")

    response = client.post(
        "/plaid/link/exchange",
        headers=_auth(),
        json={"public_token": "public-x", "institution_id": "ins_1"},
    )
    assert response.status_code == 200
    assert response.json() == {"item_id": "item-linked", "household_id": "h1"}
    assert fake_client.exchanged == ["public-x"]

    with db.begin():
        row = db.execute(
            text(
                "SELECT household_id, plaid_item_id, access_token, institution_id"
                " FROM plaid_items WHERE plaid_item_id = 'item-linked'"
            )
        ).one()
    assert row.household_id == "h1"
    assert row.access_token == "access-sandbox-tok"
    assert row.institution_id == "ins_1"


def test_a_demo_household_cannot_receive_a_real_item(client: TestClient, db) -> None:
    """KTD-10: the two planes cannot cross. A user whose only household is a demo one is refused —
    a real bank item never attaches to the demo plane."""
    with db.begin():
        _member(db, household="demo1", is_demo=True)

    response = client.post(
        "/plaid/link/exchange", headers=_auth(), json={"public_token": "public-x"}
    )
    assert response.status_code == 403
    with db.begin():
        n = db.execute(text("SELECT count(*) FROM plaid_items")).scalar()
    assert n == 0, "an item was written to a demo household"


def test_no_linkable_household_is_a_409(client: TestClient, db) -> None:
    """A verified user with no household has nowhere real to link yet (onboarding is next)."""
    with db.begin():
        db.execute(
            text("INSERT INTO users (id, stytch_user_id) VALUES ('u-linker', :s)"), {"s": STYTCH_ID}
        )

    response = client.post(
        "/plaid/link/exchange", headers=_auth(), json={"public_token": "public-x"}
    )
    assert response.status_code == 409


def test_an_ambiguous_target_is_a_409(client: TestClient, db) -> None:
    """Two non-demo households: the Link UI (next rung) will name the target; here it is refused
    rather than guessed."""
    with db.begin():
        _member(db, household="h1")
        _member(db, household="h2")

    response = client.post(
        "/plaid/link/exchange", headers=_auth(), json={"public_token": "public-x"}
    )
    assert response.status_code == 409


def test_a_viewer_cannot_link(client: TestClient, db) -> None:
    """Linking is an owner action — a viewer of a (non-demo) household cannot attach a bank."""
    with db.begin():
        _member(db, household="h1", role="viewer")

    response = client.post(
        "/plaid/link/exchange", headers=_auth(), json={"public_token": "public-x"}
    )
    assert response.status_code == 403

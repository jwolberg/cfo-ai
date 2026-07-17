"""POST /plaid/link/exchange — the first write path from outside the process.

Unlike the doorbell, this route carries our API key, and the Plaid exchange is faked so no token
leaves the test. The claims worth holding: the key is required, and a successful exchange writes an
item **scoped to the named household** through the repository (so RLS `WITH CHECK` binds it).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.auth import API_KEY_ENV, API_KEY_HEADER
from backend.plaid.deps import get_plaid_client
from tests.conftest import requires_db

pytestmark = requires_db

KEY = "test-key-not-a-real-one"


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


@pytest.fixture(autouse=True)
def secrets(monkeypatch: pytest.MonkeyPatch, app_engine: Engine) -> None:
    monkeypatch.setenv(API_KEY_ENV, KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    monkeypatch.setenv("DATABASE_URL", app_engine.url.render_as_string(hide_password=False))


@pytest.fixture
def fake_client() -> FakeLinkClient:
    return FakeLinkClient()


@pytest.fixture
def client(db, fake_client: FakeLinkClient) -> Iterator[TestClient]:
    from backend.main import app

    app.dependency_overrides[get_plaid_client] = lambda: fake_client
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _auth() -> dict[str, str]:
    return {API_KEY_HEADER: KEY}


def test_the_key_is_required(client: TestClient) -> None:
    response = client.post(
        "/plaid/link/exchange",
        json={"public_token": "public-x", "household_id": "h1"},
    )
    assert response.status_code == 401


def test_a_successful_exchange_writes_an_item_scoped_to_the_household(
    client: TestClient, fake_client: FakeLinkClient, db
) -> None:
    with db.begin():
        db.execute(text("INSERT INTO households (id, archetype) VALUES ('h1', 'test')"))

    response = client.post(
        "/plaid/link/exchange",
        headers=_auth(),
        json={"public_token": "public-x", "household_id": "h1", "institution_id": "ins_1"},
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

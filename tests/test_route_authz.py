"""Route-level authorization after the identity cutover (ticket 0048).

The IDOR suites (`tests/test_idor.py`, `tests/test_identity_schema.py`) prove the *data* layer —
RLS and the membership graph. This proves the **HTTP** layer on top of it: a verified session that
is not a member of a household is refused on every route even naming the id correctly, and
`GET /households` lists only the caller's households. This is the valid-but-non-member refusal the
whole rung exists to make pass — written here, and a hard gate again against real Stytch in U7.

Three principals, all through a stubbed verifier (the real-Stytch decode path is U7's gate):

- **reviewer** — `owner` of every seeded household (a member, sees the picker);
- **demo viewer** — `viewer` of every demo household (the public read-only principal, KTD-10);
- **outsider** — a freshly JIT-provisioned user with **zero** memberships.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.identity import stytch
from backend.identity.deps import get_verifier
from backend.seed import (
    DEMO_USER_STYTCH_ID,
    REVIEWER_USER_STYTCH_ID,
    household_id_for,
    seed_all,
)
from tests.conftest import requires_db

pytestmark = requires_db

DEMO = household_id_for("demo_biweekly")
OUTSIDER_STYTCH_ID = "stytch-outsider"

_TOKENS = {
    "reviewer": REVIEWER_USER_STYTCH_ID,
    "demo": DEMO_USER_STYTCH_ID,
    "outsider": OUTSIDER_STYTCH_ID,
}


def _verifier():
    def verify(token: str):
        if token in _TOKENS:
            return _TOKENS[token], {}
        raise stytch.StytchVerificationError("stub: unknown token")

    return verify


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def secrets(monkeypatch: pytest.MonkeyPatch, app_engine: Engine) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    monkeypatch.setenv("DATABASE_URL", app_engine.url.render_as_string(hide_password=False))


@pytest.fixture(scope="session")
def seeded(db_engine: Engine) -> None:
    with db_engine.begin() as conn:
        conn.execute(text("DELETE FROM decisions"))
        conn.execute(text("TRUNCATE households, users CASCADE"))
    seed_all(db_engine)


@pytest.fixture
def client(seeded: None) -> Iterator[TestClient]:
    from backend.main import app

    app.dependency_overrides[get_verifier] = _verifier
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_verifier, None)


class TestHouseholdsIsScopedToMembership:
    def test_a_member_sees_their_households(self, client: TestClient) -> None:
        body = client.get("/households", headers=_bearer("reviewer")).json()
        ids = {h["id"] for h in body["households"]}
        assert DEMO in ids
        assert len(ids) >= 1

    def test_an_outsider_sees_none(self, client: TestClient) -> None:
        """A freshly provisioned user with no memberships sees an empty list — not every household,
        which is exactly what the old shared key leaked (`GET /households` listed them all)."""
        body = client.get("/households", headers=_bearer("outsider")).json()
        assert body["households"] == []

    def test_no_session_is_401(self, client: TestClient) -> None:
        assert client.get("/households").status_code == 401


class TestValidButNonMemberIsRefused:
    """The refusal this rung is built to make pass. A verified session that is not a member of a
    real, existing household is refused on every route — the id being correct does not help."""

    def test_decisions(self, client: TestClient) -> None:
        assert (
            client.get(f"/households/{DEMO}/decisions", headers=_bearer("outsider")).status_code
            == 403
        )

    def test_spend(self, client: TestClient) -> None:
        assert (
            client.get(f"/households/{DEMO}/spend", headers=_bearer("outsider")).status_code == 403
        )

    def test_explain(self, client: TestClient) -> None:
        assert (
            client.get(
                f"/households/{DEMO}/decisions/2026-03-02/explain", headers=_bearer("outsider")
            ).status_code
            == 403
        )

    def test_assistant_body_id(self, client: TestClient) -> None:
        """The body-supplied id is authorized too — the one un-fixed IDOR the cutover would
        otherwise leave (KTD-2)."""
        resp = client.post(
            "/assistant/message",
            headers=_bearer("outsider"),
            json={"household_id": DEMO, "message": "hi", "history": []},
        )
        assert resp.status_code == 403


class TestTheDemoViewerReadsButThePlanesDoNotCross:
    def test_the_demo_viewer_reads_a_demo_household(self, client: TestClient) -> None:
        """The public read-only principal reaches the demo with a `viewer` session (KTD-10)."""
        assert (
            client.get(f"/households/{DEMO}/decisions", headers=_bearer("demo")).status_code == 200
        )

    def test_the_demo_viewer_sees_only_demo_households(self, client: TestClient) -> None:
        body = client.get("/households", headers=_bearer("demo")).json()
        ids = {h["id"] for h in body["households"]}
        # Every seeded household is a demo one this rung, so the viewer sees them; the guarantee
        # that matters is it can never see a *non*-demo household — proven at the data layer in
        # tests/test_link.py (a real item cannot attach to the demo plane) and by the membership
        # graph. Here: it at least reaches the demo it is meant to.
        assert DEMO in ids

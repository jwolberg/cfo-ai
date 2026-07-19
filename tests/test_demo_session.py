"""The public demo's durable session (ticket 0057): `identity/demo.py` + `POST /demo/session`.

Stytch is never called here — `_mint` is monkeypatched. What is under test is the *policy* around
it: caching (one Stytch call per window, not per request), re-minting before expiry, the
not-configured refusal, and that the route surfaces a 503 (config gap) rather than a 500.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from backend.identity import demo
from tests.conftest import requires_db


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    demo._reset_cache()
    monkeypatch.setenv("DEMO_STYTCH_EMAIL", "demo-viewer@example.com")
    monkeypatch.setenv("DEMO_STYTCH_PASSWORD", "pw")


def _count_mint(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"n": 0}

    def fake_mint(email: str, password: str) -> str:
        calls["n"] += 1
        return f"jwt-{calls['n']}"

    monkeypatch.setattr(demo, "_mint", fake_mint)
    return calls


def test_it_is_not_configured_without_the_demo_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEMO_STYTCH_EMAIL", raising=False)
    with pytest.raises(demo.DemoSessionNotConfigured):
        demo.demo_session_jwt()


def test_it_caches_the_token_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _count_mint(monkeypatch)
    first = demo.demo_session_jwt()
    second = demo.demo_session_jwt()
    assert first == second == "jwt-1"
    assert calls["n"] == 1, "a cached token must not re-hit Stytch on every request"


def test_it_remints_when_the_cached_token_is_near_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _count_mint(monkeypatch)
    demo.demo_session_jwt()  # mints jwt-1, cached with a ~5-minute expiry
    assert demo._cache is not None
    # Force the cache to look near-expiry; the next call must re-mint rather than serve it.
    demo._cache.expires_at = demo._now() + dt.timedelta(seconds=30)
    assert demo.demo_session_jwt() == "jwt-2"
    assert calls["n"] == 2


@pytest.fixture
def _app_client(monkeypatch: pytest.MonkeyPatch, app_engine: Engine) -> TestClient:
    """The app with a real DB (its startup connects), so the route can be exercised. The route
    itself touches no DB; the fixture only satisfies app startup, as the other route suites do."""
    monkeypatch.setenv("RESFI_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-real")
    monkeypatch.setenv("DATABASE_URL", app_engine.url.render_as_string(hide_password=False))
    from backend.main import app

    return TestClient(app)


@requires_db
def test_the_route_returns_a_token(
    monkeypatch: pytest.MonkeyPatch, _app_client: TestClient
) -> None:
    monkeypatch.setattr(demo, "_mint", lambda e, p: "route-jwt")
    with _app_client as c:
        resp = c.post("/demo/session")
    assert resp.status_code == 200
    assert resp.json() == {"session_jwt": "route-jwt"}


@requires_db
def test_the_route_is_503_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch, _app_client: TestClient
) -> None:
    monkeypatch.delenv("DEMO_STYTCH_EMAIL", raising=False)
    with _app_client as c:
        resp = c.post("/demo/session")
    assert resp.status_code == 503
    assert resp.json()["error"] == "demo_session_unconfigured"

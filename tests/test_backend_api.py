"""The service: what it serves, what it refuses, and what it refuses to start without.

Every test drives the app through `TestClient`, which runs the real lifespan — so "the
service refuses to start" is an assertion this file can actually make, rather than a comment.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from backend import artifact as art
from backend.auth import API_KEY_ENV, API_KEY_HEADER

KEY = "test-key-not-a-real-one"


@pytest.fixture(autouse=True)
def api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, KEY)


@pytest.fixture
def client() -> Iterator[TestClient]:
    from backend.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth() -> dict[str, str]:
    return {API_KEY_HEADER: KEY}


class TestStartup:
    def test_a_malformed_artifact_stops_the_service_coming_up(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Fail at startup, not per-request.

        A service that comes up holding bad data and answers with wrong numbers looks
        perfectly healthy to Cloud Run. One that never comes up does not.
        """
        broken = tmp_path / "decisions.json"
        broken.write_text(json.dumps({"version": 1, "days": []}))
        monkeypatch.setattr(art, "DEFAULT_PATH", broken)

        from backend.main import app

        with pytest.raises(art.ArtifactError), TestClient(app):
            pass  # pragma: no cover — the context manager raises on entry

    def test_a_missing_artifact_stops_the_service_coming_up(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setattr(art, "DEFAULT_PATH", tmp_path / "nope.json")

        from backend.main import app

        with pytest.raises(art.ArtifactError, match="cannot read artifact"), TestClient(app):
            pass  # pragma: no cover

    def test_a_missing_api_key_stops_the_service_coming_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An accidentally-public endpoint is not a default worth having."""
        monkeypatch.delenv(API_KEY_ENV, raising=False)

        from backend.main import app

        with pytest.raises(RuntimeError, match=API_KEY_ENV), TestClient(app):
            pass  # pragma: no cover


class TestHealth:
    def test_the_probe_needs_no_key(self, client: TestClient) -> None:
        """Cloud Run's health check does not carry our API key, so gating it would fail
        every deploy while the service was in fact fine."""
        response = client.get("/healthz")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestAuth:
    def test_no_key_is_refused(self, client: TestClient) -> None:
        assert client.get("/decisions").status_code == 401

    def test_a_wrong_key_is_refused(self, client: TestClient) -> None:
        assert client.get("/decisions", headers={API_KEY_HEADER: "wrong"}).status_code == 401

    def test_absence_and_error_are_indistinguishable(self, client: TestClient) -> None:
        """A caller who guesses wrong learns nothing about whether they guessed at all."""
        missing = client.get("/decisions")
        wrong = client.get("/decisions", headers={API_KEY_HEADER: "wrong"})

        assert missing.status_code == wrong.status_code
        assert missing.json() == wrong.json()


class TestDecisions:
    def test_the_window_comes_back_newest_first(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """The order the feed reads in — R2 is a reverse-chronological feed."""
        body = client.get("/decisions", headers=auth).json()
        dates = [d["date"] for d in body["decisions"]]

        assert dates == sorted(dates, reverse=True)
        assert dates[0] == body["window"]["end"]
        assert dates[-1] == body["window"]["start"]

    def test_the_demo_has_a_fixed_today(self, client: TestClient, auth: dict[str, str]) -> None:
        """ "Today" is the last served day, not the wall clock. Relative dates ("last
        Tuesday") have to resolve against the demo's window or they mean nothing."""
        body = client.get("/decisions", headers=auth).json()

        assert body["window"]["today"] == body["window"]["end"]

    def test_money_crosses_the_wire_as_text(self, client: TestClient, auth: dict[str, str]) -> None:
        """JSON has one number type and it is a double. A cent that round-trips through a
        double is no longer the cent the engine decided on."""
        raw = client.get("/decisions", headers=auth).text
        body = json.loads(raw)

        for record in body["decisions"]:
            assert isinstance(record["amount"], str)
            assert isinstance(record["checking_balance"], str)
            assert "." in record["amount"]  # "400.00", never "400"

    def test_the_engine_both_acts_and_declines(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        body = client.get("/decisions", headers=auth).json()
        actions = {d["action"] for d in body["decisions"]}

        assert actions == {"sweep", "refuse"}

    def test_every_decision_carries_its_reason_codes(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """Codes, not prose. The sentence is a rendering of the fact; the fact is the code."""
        body = client.get("/decisions", headers=auth).json()

        assert all(d["reason_codes"] for d in body["decisions"])

"""The service: what it serves, what it refuses, and what it refuses to start without.

Every test drives the app through `TestClient`, which runs the real lifespan — so "the
service refuses to start" is an assertion this file can actually make, rather than a comment.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal

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


class TestSummary:
    def test_the_headline_stats_are_there(self, client: TestClient, auth: dict[str, str]) -> None:
        summary = client.get("/decisions", headers=auth).json()["summary"]

        assert summary["sweep_count"] > 0
        assert summary["refuse_count"] > 0
        assert Decimal(summary["interest_avoided_total"]) > 0
        assert Decimal(summary["current_buffer"]) > 0
        assert summary["paid_off"] is False

    def test_the_counts_account_for_every_day(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        body = client.get("/decisions", headers=auth).json()
        summary = body["summary"]

        assert summary["sweep_count"] + summary["refuse_count"] == len(body["decisions"])

    def test_interest_avoided_is_the_engines_own_claim(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """Summed from the reasons the engine emitted, never recomputed here.

        `engine/interest.py` declines to make the claim when it cannot stand behind it. A
        second implementation of the number the company is graded on would eventually
        disagree with the first, and nobody would know which one was in the UI.
        """
        body = client.get("/decisions", headers=auth).json()
        claimed = sum(
            Decimal(reason["text"].split("about $")[1].split(" of interest")[0].replace(",", ""))
            for decision in body["decisions"]
            for reason in decision["reasons"]
            if reason["code"] == "interest_avoided"
        )

        assert Decimal(body["summary"]["interest_avoided_total"]) == claimed


class TestExplain:
    def sweep_date(self, client: TestClient, auth: dict[str, str]) -> str:
        body = client.get("/decisions", headers=auth).json()
        return next(d["date"] for d in body["decisions"] if d["action"] == "sweep")

    def refusal_date(self, client: TestClient, auth: dict[str, str]) -> str:
        body = client.get("/decisions", headers=auth).json()
        return next(d["date"] for d in body["decisions"] if d["action"] == "refuse")

    def test_a_refusal_reads_as_english_not_as_a_code(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """Covers AE2. A refusal is not an error and must not read like one — the user is
        being told their money is staying put, and why."""
        day = self.refusal_date(client, auth)

        body = client.get(f"/decisions/{day}/explain", headers=auth).json()

        assert body["narration"]
        assert all(len(sentence.split()) > 3 for sentence in body["narration"])
        # No raw ReasonCode leaks into the prose.
        assert not any("_" in sentence for sentence in body["narration"])

    def test_every_served_day_narrates(self, client: TestClient, auth: dict[str, str]) -> None:
        """Not a spot check: every date in the window has to resolve to real copy, because
        every one of them is tappable in the feed."""
        body = client.get("/decisions", headers=auth).json()

        for decision in body["decisions"]:
            narration = client.get(f"/decisions/{decision['date']}/explain", headers=auth).json()
            assert narration["narration"], f"{decision['date']} narrated to nothing"

    def test_a_sweep_says_what_it_saved(self, client: TestClient, auth: dict[str, str]) -> None:
        day = self.sweep_date(client, auth)

        body = client.get(f"/decisions/{day}/explain", headers=auth).json()

        assert body["action"] == "sweep"
        assert "interest_avoided" in body["reason_codes"]
        assert any("interest you won't pay" in s for s in body["narration"])

    def test_a_day_outside_the_window_is_no_record_not_an_error(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """Same posture as the assistant's tools: we have nothing on record, and that is an
        answer, not a failure. A 500 would say the service is broken. It is not."""
        response = client.get("/decisions/2025-01-01/explain", headers=auth)

        assert response.status_code == 404
        assert response.json()["error"] == "no_record"

    def test_a_warm_up_day_is_no_record_too(self, client: TestClient, auth: dict[str, str]) -> None:
        """The warm-up runway was decided but never served. To the user it is simply a day we
        have nothing on — indistinguishable from any other day outside the window."""
        response = client.get("/decisions/2026-02-01/explain", headers=auth)

        assert response.status_code == 404
        assert response.json()["error"] == "no_record"

    def test_a_nonsense_date_is_no_record_too(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        response = client.get("/decisions/not-a-date/explain", headers=auth)

        assert response.status_code == 404
        assert response.json()["error"] == "no_record"

    def test_narration_still_needs_a_key(self, client: TestClient) -> None:
        assert client.get("/decisions/2026-03-02/explain").status_code == 401

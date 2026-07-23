"""The operator console — the god-mode gate, the lookup, and the snapshot behind a decision.

Needs a real Postgres (the seeded households live there), and says so loudly via `requires_db`. The
console is the operator's only window into a live money-moving service, so the two properties that
matter are: **nothing is reachable without the password**, and **what it shows is the engine's real
trace**, not a plausible retelling.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.operator import OperatorNotConfigured, create_operator_app, decision_trace, list_status
from backend.seed import seed_all
from tests.conftest import requires_db

pytestmark = requires_db

PASSWORD = "god-mode-test"


@pytest.fixture
def seeded_engine(db_engine, monkeypatch):
    monkeypatch.setenv("OPERATOR_PASSWORD", PASSWORD)
    monkeypatch.setenv("OPERATOR_USER", "operator")
    with db_engine.begin() as c:
        c.execute(text("DELETE FROM decisions"))
        c.execute(text("TRUNCATE households CASCADE"))
    seed_all(db_engine)
    return db_engine


@pytest.fixture
def client(seeded_engine):
    return TestClient(create_operator_app(seeded_engine))


def _login(client: TestClient) -> None:
    r = client.post(
        "/login", data={"user": "operator", "password": PASSWORD}, follow_redirects=False
    )
    assert r.status_code == 303


class TestTheGate:
    def test_the_console_refuses_to_start_without_a_password(self, monkeypatch) -> None:
        monkeypatch.delenv("OPERATOR_PASSWORD", raising=False)
        with pytest.raises(OperatorNotConfigured):
            create_operator_app(object())  # never reaches engine use — the guard is first

    def test_dashboard_is_unreachable_without_a_session(self, client) -> None:
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"

    def test_a_household_page_is_unreachable_without_a_session(self, client) -> None:
        r = client.get("/household/hh_demo_biweekly", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"

    def test_the_wrong_password_is_refused(self, client) -> None:
        r = client.post(
            "/login", data={"user": "operator", "password": "wrong"}, follow_redirects=False
        )
        assert r.status_code == 401

    def test_a_forged_cookie_is_refused(self, client) -> None:
        client.cookies.set("op_session", "not.a.real.token")
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"


class TestLookup:
    def test_login_then_the_dashboard_lists_every_household(self, client) -> None:
        _login(client)
        r = client.get("/")
        assert r.status_code == 200
        for hid in ("hh_demo_biweekly", "hh_monthly_thin", "hh_semimonthly_portfolio"):
            assert hid in r.text

    def test_list_status_reports_the_last_decision(self, seeded_engine) -> None:
        rows = {s.id: s for s in list_status(seeded_engine)}
        biweekly = rows["hh_demo_biweekly"]
        assert biweekly.last_action == "refuse"  # the window ends on a refusal
        assert biweekly.paused is False and biweekly.blackout_dates == []


class TestTheSnapshotBehindADecision:
    def test_the_sweep_day_shows_the_full_gate_trace(self, client) -> None:
        _login(client)
        r = client.get("/household/hh_demo_biweekly/decision/2026-05-25")
        assert r.status_code == 200
        # the arithmetic, the outcome, and a gate name — the real trace, not a summary
        assert "449.50" in r.text
        assert "swept" in r.text.lower()
        assert "Cadence" in r.text

    def test_the_trace_is_the_engines_own(self, seeded_engine) -> None:
        from datetime import date

        t = decision_trace(seeded_engine, "hh_demo_biweekly", date(2026, 5, 25))
        assert t is not None
        assert (t.action, str(t.amount)) == ("sweep", "449.50")
        assert t.steps[-1].stage == "Decision"  # a sweep terminates on the Decision step

    def test_a_missing_snapshot_is_none_not_a_crash(self, seeded_engine) -> None:
        from datetime import date

        assert decision_trace(seeded_engine, "hh_demo_biweekly", date(1999, 1, 1)) is None

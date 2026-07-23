"""The operator console — the god-mode gate, the lookup, and the snapshot behind a decision.

Needs a real Postgres (the seeded households live there), and says so loudly via `requires_db`. The
console is the operator's only window into a live money-moving service, so the two properties that
matter are: **nothing is reachable without the password**, and **what it shows is the engine's real
trace**, not a plausible retelling.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from backend.db.repository import repository
from backend.db.snapshots import PostgresSnapshotStore
from backend.operator import (
    OperatorNotConfigured,
    SystemHalted,
    account_facts,
    assert_not_halted,
    create_operator_app,
    decision_trace,
    is_globally_halted,
    list_status,
    pause_household,
    recent_actions,
    set_global_halt,
    unpause_household,
)
from backend.seed import seed_all
from backend.trace import trace
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
        # Platform-level, like `users`: `households CASCADE` never reaches it, so a halt logged by
        # one test would leak its state into the next. Clear it explicitly.
        c.execute(text("TRUNCATE operator_actions"))
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
        t = decision_trace(seeded_engine, "hh_demo_biweekly", date(2026, 5, 25))
        assert t is not None
        assert (t.action, str(t.amount)) == ("sweep", "449.50")
        assert t.steps[-1].stage == "Decision"  # a sweep terminates on the Decision step

    def test_a_missing_snapshot_is_none_not_a_crash(self, seeded_engine) -> None:
        assert decision_trace(seeded_engine, "hh_demo_biweekly", date(1999, 1, 1)) is None


class TestAccountFacts:
    def test_facts_carry_accounts_income_and_every_card(self, seeded_engine) -> None:
        f = account_facts(seeded_engine, "hh_monthly_thin", date(2026, 5, 21))
        assert f is not None
        # two cards, each with a balance and a reported APR
        assert {c.card_id for c in f.cards} == {"card_c_high", "card_c_low"}
        high = next(c for c in f.cards if c.card_id == "card_c_high")
        assert high.balance.startswith("$") and "22.99%" in high.apr
        # the income variation the decision saw, and a funding account
        assert f.income_variation == "0.74%"
        assert any("(funding)" in label for label, _bal, _note in f.accounts)

    def test_the_facts_section_renders_the_card_table(self, client) -> None:
        _login(client)
        r = client.get("/household/hh_monthly_thin/decision/2026-05-21")
        assert "Account facts" in r.text
        assert "card_c_high" in r.text and "card_c_low" in r.text
        assert "3 trailing" in r.text  # the income measurement note

    def test_missing_snapshot_facts_are_none(self, seeded_engine) -> None:
        assert account_facts(seeded_engine, "hh_demo_biweekly", date(1999, 1, 1)) is None


class TestPause:
    HID = "hh_demo_biweekly"

    def test_pause_blacks_out_today_forward_and_logs_it(self, seeded_engine) -> None:
        today = date(2026, 8, 1)
        pause_household(seeded_engine, "operator", self.HID, today=today)

        status = {s.id: s for s in list_status(seeded_engine)}[self.HID]
        assert status.paused is True
        # today and a future day are blacked out; a past day is not
        assert today.isoformat() in status.blackout_dates
        assert (today + timedelta(days=10)).isoformat() in status.blackout_dates
        assert (today - timedelta(days=1)).isoformat() not in status.blackout_dates

        latest = recent_actions(seeded_engine, limit=1)[0]
        assert latest.action == "pause" and latest.household_id == self.HID

    def test_pause_actually_refuses_the_engine(self, seeded_engine) -> None:
        """The pause is not cosmetic: a paused day, re-decided, is a BLACKOUT refusal. Uses a day
        that has a stored snapshot so the engine can be re-run over the real inputs."""
        day = date(2026, 5, 25)  # the sweep day
        pause_household(seeded_engine, "operator", self.HID, today=day, horizon_days=1)
        with repository(seeded_engine, self.HID) as repo:
            snap = PostgresSnapshotStore(repo.conn).get(f"pg:{self.HID}:{day.isoformat()}")
            policy = repo.policy()
        # the snapshot the engine saw, with the operator's new blackout applied
        paused_snap = replace(
            snap, policy=replace(snap.policy, blackout_dates=frozenset(policy["blackout_dates"]))
        )
        assert trace(paused_snap).action == "refuse"

    def test_unpause_clears_the_forward_blackout_and_logs_it(self, seeded_engine) -> None:
        today = date(2026, 8, 1)
        pause_household(seeded_engine, "operator", self.HID, today=today)
        unpause_household(seeded_engine, "operator", self.HID, today=today)

        status = {s.id: s for s in list_status(seeded_engine)}[self.HID]
        assert status.paused is False and status.blackout_dates == []
        assert recent_actions(seeded_engine, limit=1)[0].action == "unpause"


class TestGlobalHalt:
    def test_halt_state_is_derived_from_the_log(self, seeded_engine) -> None:
        assert is_globally_halted(seeded_engine) is False
        set_global_halt(seeded_engine, "operator", True)
        assert is_globally_halted(seeded_engine) is True
        set_global_halt(seeded_engine, "operator", False)
        assert is_globally_halted(seeded_engine) is False  # latest row wins

    def test_the_guard_raises_only_while_halted(self, seeded_engine) -> None:
        set_global_halt(seeded_engine, "operator", True)
        with pytest.raises(SystemHalted):
            assert_not_halted(seeded_engine)
        set_global_halt(seeded_engine, "operator", False)
        assert_not_halted(seeded_engine)  # no raise

    def test_a_global_action_names_no_household(self, seeded_engine) -> None:
        set_global_halt(seeded_engine, "operator", True)
        assert recent_actions(seeded_engine, limit=1)[0].household_id is None


class TestTheLogIsAppendOnly:
    def test_the_app_role_cannot_delete_an_action(self, app_engine, seeded_engine) -> None:
        """The audit record is append-only by grant, not by convention: even the app role that
        writes it holds no DELETE. `app_engine` is the non-superuser the service runs as."""
        from psycopg.errors import InsufficientPrivilege

        set_global_halt(seeded_engine, "operator", True)
        with pytest.raises(DBAPIError) as caught, app_engine.begin() as conn:
            conn.execute(text("DELETE FROM operator_actions"))
        assert isinstance(caught.value.orig, InsufficientPrivilege)

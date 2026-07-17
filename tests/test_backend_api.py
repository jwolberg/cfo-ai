"""The service: what it serves, what it refuses, and what it refuses to start without.

Every test drives the app through `TestClient`, which runs the real lifespan — so "the
service refuses to start" is an assertion this file can actually make, rather than a comment.

**These need Postgres now, and that is ticket `0024` landing.** The service reads households from
the database; a suite that could still run without one would be testing a service that no longer
exists. It also runs the app as `cfo_test` — a non-superuser — rather than as the owner, because
`assert_rls_binds()` refuses to start under a role that bypasses RLS, and a test that started the
app as a superuser would be exercising a service whose scoping is off.

The seeding is done as the owner and the *serving* as the app role, which is the split production
has: a seeder is an admin task, the service is not.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend import artifact as art
from backend.auth import API_KEY_ENV, API_KEY_HEADER
from backend.seed import household_id_for, seed_all
from tests.conftest import requires_db

pytestmark = requires_db

KEY = "test-key-not-a-real-one"

# The demo household, by the id the seeder derives. Archetype A is the oracle here for the third
# time: `0019` proved the walk did not drift, `0023` proved the seeder wrote what the walk decided,
# and this file proves the read path serves what the seeder wrote.
DEMO = household_id_for("demo_biweekly")


@pytest.fixture(autouse=True)
def secrets(monkeypatch: pytest.MonkeyPatch, app_engine: Engine) -> None:
    """Everything the service refuses to start without. Neither key is ever used to call out.

    `DATABASE_URL` is here rather than in `client` because `TestStartup` builds its own
    `TestClient` to watch the lifespan fail — and each of those tests is about *one* missing thing.
    Without a database they would all fail on the database and pass for the wrong reason.
    """
    monkeypatch.setenv(API_KEY_ENV, KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    monkeypatch.setenv("DATABASE_URL", app_engine.url.render_as_string(hide_password=False))


@pytest.fixture(scope="session")
def seeded(db_engine: Engine) -> None:
    """Every archetype, once per session. Written as the owner; served as `cfo_test`."""
    with db_engine.begin() as conn:
        conn.execute(text("DELETE FROM decisions"))
        conn.execute(text("TRUNCATE households CASCADE"))
    seed_all(db_engine)


@pytest.fixture
def client(seeded: None) -> Iterator[TestClient]:
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

    def test_a_missing_anthropic_key_stops_the_service_coming_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail at deploy, not at the moment a user opens the modal and asks a question."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        from backend.main import app

        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"), TestClient(app):
            pass  # pragma: no cover

    def test_an_unreachable_database_stops_the_service_coming_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-0004 [3.2]. The households are the product now — a service that cannot reach them
        has nothing to serve, and finding that out per-request means answering 500s one user at a
        time while Cloud Run reports the container as healthy."""
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://nobody@127.0.0.1:1/does-not-exist")

        from backend.main import app

        with pytest.raises(Exception), TestClient(app):  # noqa: B017 — driver-specific
            pass  # pragma: no cover

    def test_an_unmigrated_database_is_refused(self, db_engine: Engine) -> None:
        """Reachable is not the same as usable. A database that answers `SELECT 1` and has no
        `decisions` table is a service that comes up and 500s on its first real request.

        The tables are hidden with `search_path` rather than dropped: the assertion is about what
        the read path can *reach*, which is exactly what an empty search path takes away, and
        dropping a partitioned table to prove a startup check would be a strange trade.
        """
        from backend.main import _assert_migrated

        with db_engine.connect() as conn, conn.begin() as tx:
            conn.execute(text("SET LOCAL search_path TO pg_temp"))

            with pytest.raises(RuntimeError, match="alembic upgrade head"):
                _assert_migrated(conn)

            tx.rollback()

    def test_a_role_that_bypasses_rls_stops_the_service_coming_up(
        self, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one a ping would miss, and the one that already bit.

        `db_engine` is the superuser that owns the tables — reachable, migrated, and completely
        unable to enforce row-level security. Neon's default role has `rolbypassrls` for the same
        effect, so this is the connection string a deploy is most likely to be handed. Under it a
        household-scoped query returns **every** household while the IDOR suite stays green.
        """
        monkeypatch.setenv("DATABASE_URL", db_engine.url.render_as_string(hide_password=False))

        from backend.db.session import RlsWouldNotBind
        from backend.main import app

        with pytest.raises(RlsWouldNotBind), TestClient(app):
            pass  # pragma: no cover


class TestHealth:
    def test_the_probe_needs_no_key(self, client: TestClient) -> None:
        """Cloud Run's health check does not carry our API key, so gating it would fail
        every deploy while the service was in fact fine."""
        response = client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_the_probe_is_not_named_healthz(self, client: TestClient) -> None:
        """`/healthz` is reserved by Google's frontend on `*.run.app` — it answers with its own
        404 and the request never reaches the container. A route by that name would look correct
        in every test and be unreachable in production, so assert we did not reintroduce it."""
        assert client.get("/healthz").status_code == 404


class TestAuth:
    def test_no_key_is_refused(self, client: TestClient) -> None:
        assert client.get(f"/households/{DEMO}/decisions").status_code == 401

    def test_a_wrong_key_is_refused(self, client: TestClient) -> None:
        assert (
            client.get(
                f"/households/{DEMO}/decisions", headers={API_KEY_HEADER: "wrong"}
            ).status_code
            == 401
        )

    def test_absence_and_error_are_indistinguishable(self, client: TestClient) -> None:
        """A caller who guesses wrong learns nothing about whether they guessed at all."""
        missing = client.get(f"/households/{DEMO}/decisions")
        wrong = client.get(f"/households/{DEMO}/decisions", headers={API_KEY_HEADER: "wrong"})

        assert missing.status_code == wrong.status_code
        assert missing.json() == wrong.json()


class TestDecisions:
    def test_the_window_comes_back_newest_first(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """The order the feed reads in — R2 is a reverse-chronological feed."""
        body = client.get(f"/households/{DEMO}/decisions", headers=auth).json()
        dates = [d["date"] for d in body["decisions"]]

        assert dates == sorted(dates, reverse=True)
        assert dates[0] == body["window"]["end"]
        assert dates[-1] == body["window"]["start"]

    def test_the_demo_has_a_fixed_today(self, client: TestClient, auth: dict[str, str]) -> None:
        """ "Today" is the last served day, not the wall clock. Relative dates ("last
        Tuesday") have to resolve against the demo's window or they mean nothing."""
        body = client.get(f"/households/{DEMO}/decisions", headers=auth).json()

        assert body["window"]["today"] == body["window"]["end"]

    def test_money_crosses_the_wire_as_text(self, client: TestClient, auth: dict[str, str]) -> None:
        """JSON has one number type and it is a double. A cent that round-trips through a
        double is no longer the cent the engine decided on."""
        raw = client.get(f"/households/{DEMO}/decisions", headers=auth).text
        body = json.loads(raw)

        for record in body["decisions"]:
            assert isinstance(record["amount"], str)
            assert isinstance(record["checking_balance"], str)
            assert "." in record["amount"]  # "400.00", never "400"

    def test_the_engine_both_acts_and_declines(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        body = client.get(f"/households/{DEMO}/decisions", headers=auth).json()
        actions = {d["action"] for d in body["decisions"]}

        assert actions == {"sweep", "refuse"}

    def test_every_decision_carries_its_reason_codes(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """Codes, not prose. The sentence is a rendering of the fact; the fact is the code."""
        body = client.get(f"/households/{DEMO}/decisions", headers=auth).json()

        assert all(d["reason_codes"] for d in body["decisions"])


class TestSummary:
    def test_the_headline_stats_are_there(self, client: TestClient, auth: dict[str, str]) -> None:
        summary = client.get(f"/households/{DEMO}/decisions", headers=auth).json()["summary"]

        assert summary["sweep_count"] > 0
        assert summary["refuse_count"] > 0
        assert Decimal(summary["interest_avoided_total"]) > 0
        assert Decimal(summary["current_buffer"]) > 0
        assert summary["paid_off"] is False

    def test_the_counts_account_for_every_day(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        body = client.get(f"/households/{DEMO}/decisions", headers=auth).json()
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
        body = client.get(f"/households/{DEMO}/decisions", headers=auth).json()
        claimed = sum(
            Decimal(reason["text"].split("about $")[1].split(" of interest")[0].replace(",", ""))
            for decision in body["decisions"]
            for reason in decision["reasons"]
            if reason["code"] == "interest_avoided"
        )

        assert Decimal(body["summary"]["interest_avoided_total"]) == claimed


class TestExplain:
    def sweep_date(self, client: TestClient, auth: dict[str, str]) -> str:
        body = client.get(f"/households/{DEMO}/decisions", headers=auth).json()
        return next(d["date"] for d in body["decisions"] if d["action"] == "sweep")

    def refusal_date(self, client: TestClient, auth: dict[str, str]) -> str:
        body = client.get(f"/households/{DEMO}/decisions", headers=auth).json()
        return next(d["date"] for d in body["decisions"] if d["action"] == "refuse")

    def test_a_refusal_reads_as_english_not_as_a_code(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """Covers AE2. A refusal is not an error and must not read like one — the user is
        being told their money is staying put, and why."""
        day = self.refusal_date(client, auth)

        body = client.get(f"/households/{DEMO}/decisions/{day}/explain", headers=auth).json()

        assert body["narration"]
        assert all(len(sentence.split()) > 3 for sentence in body["narration"])
        # No raw ReasonCode leaks into the prose.
        assert not any("_" in sentence for sentence in body["narration"])

    def test_every_served_day_narrates(self, client: TestClient, auth: dict[str, str]) -> None:
        """Not a spot check: every date in the window has to resolve to real copy, because
        every one of them is tappable in the feed."""
        body = client.get(f"/households/{DEMO}/decisions", headers=auth).json()

        for decision in body["decisions"]:
            narration = client.get(
                f"/households/{DEMO}/decisions/{decision['date']}/explain", headers=auth
            ).json()
            assert narration["narration"], f"{decision['date']} narrated to nothing"

    def test_a_sweep_says_what_it_saved(self, client: TestClient, auth: dict[str, str]) -> None:
        day = self.sweep_date(client, auth)

        body = client.get(f"/households/{DEMO}/decisions/{day}/explain", headers=auth).json()

        assert body["action"] == "sweep"
        assert "interest_avoided" in body["reason_codes"]
        assert any("interest you won't pay" in s for s in body["narration"])

    def test_a_day_outside_the_window_is_no_record_not_an_error(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """Same posture as the assistant's tools: we have nothing on record, and that is an
        answer, not a failure. A 500 would say the service is broken. It is not."""
        response = client.get(f"/households/{DEMO}/decisions/2025-01-01/explain", headers=auth)

        assert response.status_code == 404
        assert response.json()["error"] == "no_record"

    def test_a_warm_up_day_is_no_record_too(self, client: TestClient, auth: dict[str, str]) -> None:
        """The warm-up runway was decided but never served. To the user it is simply a day we
        have nothing on — indistinguishable from any other day outside the window."""
        response = client.get(f"/households/{DEMO}/decisions/2026-02-01/explain", headers=auth)

        assert response.status_code == 404
        assert response.json()["error"] == "no_record"

    def test_a_nonsense_date_is_no_record_too(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        response = client.get(f"/households/{DEMO}/decisions/not-a-date/explain", headers=auth)

        assert response.status_code == 404
        assert response.json()["error"] == "no_record"

    def test_narration_still_needs_a_key(self, client: TestClient) -> None:
        assert client.get(f"/households/{DEMO}/decisions/2026-03-02/explain").status_code == 401


class TestAssistantEndpoint:
    """The endpoint wiring only. The guard's own behaviour is `tests/test_assistant.py`."""

    def fake_model(self, client: TestClient, *responses):
        from tests.test_assistant import FakeClient

        fake = FakeClient(*responses)
        client.app.state.assistant = fake
        return fake

    def test_a_question_gets_a_narrated_answer(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        from tests.test_assistant import says

        self.fake_model(client, says("Which day did you mean?"))

        response = client.post(
            "/assistant/message",
            headers=auth,
            json={
                "household_id": DEMO,
                "message": "why didn't you pay last Tuesday?",
                "history": [],
            },
        )

        assert response.status_code == 200
        assert response.json() == {"reply": "Which day did you mean?", "outcome": "answered"}

    def test_the_outcome_distinguishes_a_hallucination_from_an_outage(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """Two failures that read alike to the user and are nothing alike to us. The client
        gets both the copy and the reason, so a future logging pass can tell them apart."""
        from tests.test_assistant import says

        self.fake_model(client, says("On 2026-03-02 we paid $9,999.00."))

        body = client.post(
            "/assistant/message",
            headers=auth,
            json={"household_id": DEMO, "message": "how much?", "history": []},
        ).json()

        assert body["outcome"] == "no_record"  # never fetched it — nothing behind the claim
        assert "9,999" not in body["reply"]

    def test_an_empty_message_is_rejected(self, client: TestClient, auth: dict[str, str]) -> None:
        response = client.post(
            "/assistant/message",
            headers=auth,
            json={"household_id": DEMO, "message": "", "history": []},
        )

        assert response.status_code == 422

    def test_the_assistant_needs_a_key(self, client: TestClient) -> None:
        """The one endpoint that costs money per call is not the one to leave open."""
        response = client.post(
            "/assistant/message", json={"household_id": DEMO, "message": "hi", "history": []}
        )

        assert response.status_code == 401


class TestSpend:
    """`GET /spend` — comprehension, not a decision.

    Nothing served here feeds the engine. The rolling series is the exact structure that will
    eventually replace `daily_discretionary_high` in the forecast, rendered a release *before* it
    is trusted with a decision — so it earns its way in having already been looked at.
    """

    def test_it_requires_an_api_key(self, client: TestClient) -> None:
        assert client.get("/spend").status_code == 401

    def test_the_two_obligations_are_reported_separately(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """They fall due a **month apart**. A single "what you owe" figure hides exactly the
        thing the user needs to see: what is committed, and what is quietly forming behind it."""
        body = client.get("/spend", headers=auth).json()
        cycle = body["this_cycle"]

        assert cycle["statement"]["reserved"] is True
        assert cycle["unbilled"]["reserved"] is False
        # The unbilled statement comes due strictly later — that is what makes it unbilled.
        assert cycle["unbilled"]["due"] > cycle["statement"]["due"]

    def test_the_reserve_is_shown_so_it_does_not_look_arbitrary(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """ "We're holding back $X of your cash for this." The line that ties the dashboard to
        the engine."""
        body = client.get("/spend", headers=auth).json()
        assert body["this_cycle"]["held_back"] is not None

    def test_every_money_field_crosses_the_wire_as_a_string(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """A float here is a rounding bug with a long fuse. The convention is repo-wide."""
        body = client.get("/spend", headers=auth).json()

        money_fields = [
            body["this_cycle"]["statement"]["amount"],
            body["this_cycle"]["unbilled"]["amount"],
            body["this_cycle"]["held_back"],
            body["last_cycle"]["charged"],
            body["last_cycle"]["paid"],
            body["last_cycle"]["grew_by"],
            body["normal"]["worst_30d_cash"],
            body["normal"]["worst_30d_card"],
            *body["normal"]["rolling_30d_cash"],
            *body["normal"]["rolling_30d_card"],
        ]
        for value in money_fields:
            assert isinstance(value, str), f"{value!r} crossed the wire as a number"

    def test_the_worst_window_is_the_worst_of_the_series(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        from decimal import Decimal

        body = client.get("/spend", headers=auth).json()
        series = [Decimal(v) for v in body["normal"]["rolling_30d_cash"]]

        assert series, "a 90-day window has overlapping 30-day totals"
        assert Decimal(body["normal"]["worst_30d_cash"]) == max(series)

    def test_a_growing_card_is_reported_as_growing(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """If charges outran payments the sweep is not the answer, and `grew_by` is how the
        product knows to say so instead of staying quiet about it."""
        from decimal import Decimal

        body = client.get("/spend", headers=auth).json()
        last = body["last_cycle"]

        assert Decimal(last["grew_by"]) == Decimal(last["charged"]) - Decimal(last["paid"])


class TestTheHouseholdList:
    """`GET /households` — the switcher's source, and the one query that is not scoped."""

    def test_every_archetype_is_listed(self, client: TestClient, auth: dict[str, str]) -> None:
        from backend.archetypes import ARCHETYPES

        body = client.get("/households", headers=auth).json()

        assert {h["archetype"] for h in body["households"]} == set(ARCHETYPES)

    def test_each_carries_a_label_a_human_can_pick_between(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        body = client.get("/households", headers=auth).json()

        for h in body["households"]:
            assert h["label"], f"{h['id']} has no label"
            assert h["label"] != h["id"], f"{h['id']}'s label is just its id"

    def test_it_carries_nothing_an_id_should_not_buy(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """No balances, no decisions, no counts. This is the query you ask *before* you have a
        household to scope to, so it must not be a way to learn anything about one."""
        body = client.get("/households", headers=auth).json()

        for h in body["households"]:
            assert set(h) == {"id", "archetype", "label"}

    def test_it_needs_a_key(self, client: TestClient) -> None:
        assert client.get("/households").status_code == 401


class TestAnUnknownHousehold:
    """A 404 — not a 500, and emphatically not an empty 200."""

    def test_decisions_for_a_household_we_have_nothing_on(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        response = client.get("/households/hh_not_a_household/decisions", headers=auth)

        assert response.status_code == 404
        assert response.json()["error"] == "no_household"

    def test_it_is_not_an_empty_two_hundred(self, client: TestClient, auth: dict[str, str]) -> None:
        """`decisions: []` would say this household exists and the engine decided nothing for it.
        That is a different claim, and a false one."""
        response = client.get("/households/hh_not_a_household/decisions", headers=auth)

        assert response.status_code != 200
        assert "decisions" not in response.json()

    def test_the_assistant_refuses_it_too(self, client: TestClient, auth: dict[str, str]) -> None:
        response = client.post(
            "/assistant/message",
            headers=auth,
            json={"household_id": "hh_not_a_household", "message": "hi", "history": []},
        )

        assert response.status_code == 404

    def test_an_unknown_household_still_needs_a_key(self, client: TestClient) -> None:
        """Auth before existence: a caller without a key must not be able to probe which
        households exist by reading the difference between a 401 and a 404."""
        assert client.get("/households/hh_not_a_household/decisions").status_code == 401


class TestTheReadPathServesWhatTheFileDid:
    """Archetype A is the oracle for the third time.

    `0019` proved the walk did not drift. `0023` proved the seeder wrote what the walk decided.
    This proves the read path serves what the seeder wrote — so the whole chain from `sim/` to the
    wire is pinned end to end, and the source moved without the answer moving.
    """

    def test_the_demo_feed_is_the_committed_artifact_day_for_day(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        committed = art.load()
        body = client.get(f"/households/{DEMO}/decisions", headers=auth).json()
        served = list(reversed(body["decisions"]))

        assert len(served) == len(committed.days)

        for got, expected in zip(served, committed.days, strict=True):
            assert got["date"] == expected.day.isoformat()
            assert got["action"] == expected.decision.action.value
            assert Decimal(got["amount"]) == expected.decision.amount
            assert got["target_debt_id"] == expected.decision.target_debt_id
            assert got["reason_codes"] == [r.code.value for r in expected.decision.reasons]
            assert Decimal(got["checking_balance"]) == expected.checking_balance
            assert Decimal(got["debt_balance"]) == expected.debt_balance

    def test_the_summary_is_the_committed_artifacts(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        committed = art.load()
        summary = client.get(f"/households/{DEMO}/decisions", headers=auth).json()["summary"]

        assert (
            Decimal(summary["interest_avoided_total"]) == committed.summary.interest_avoided_total
        )
        assert Decimal(summary["total_swept"]) == committed.summary.total_swept
        assert summary["sweep_count"] == committed.summary.sweep_count
        assert summary["refuse_count"] == committed.summary.refuse_count
        assert summary["targeted_debt_id"] == committed.summary.targeted_debt_id
        assert Decimal(summary["starting_debt_balance"]) == committed.summary.starting_debt_balance

    def test_a_portfolio_household_serves_every_card(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """The thing the file could never do. `build()` refused a portfolio until `0030`, and
        `DayRecord` carried one debt — so this is the first feed that has ever shown three."""
        household = household_id_for("semimonthly_portfolio")
        body = client.get(f"/households/{household}/decisions", headers=auth).json()

        for day in body["decisions"]:
            ids = {d["debt_id"] for d in day["debts"]}
            assert ids == {"card_b_high", "card_b_low", "card_b_transactor"}, day["date"]

    def test_an_estimated_rate_reaches_the_client_as_an_estimate(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """`0028`, all the way to the wire. A 23% estimate and a reported 23% are the same number,
        and a client that cannot tell them apart will render the guess as a fact."""
        household = household_id_for("apr_unreported")
        body = client.get(f"/households/{household}/decisions", headers=auth).json()

        for day in body["decisions"]:
            for debt in day["debts"]:
                assert debt["apr_source"] == "estimated", day["date"]
                assert debt["apr"] == "0.23"


class TestScoping:
    """The repository and RLS both bind the id in the path. This is the read path's own IDOR
    check — `tests/test_idor.py` proves each layer with the other removed; this proves the route
    that finally has callers is wired to them at all."""

    def test_a_household_only_ever_sees_its_own_decisions(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        for name in ("demo_biweekly", "semimonthly_portfolio", "monthly_thin"):
            household = household_id_for(name)
            body = client.get(f"/households/{household}/decisions", headers=auth).json()
            cards = {d["debt_id"] for day in body["decisions"] for d in day["debts"]}

            other = {"card_demo"} if name != "demo_biweekly" else {"card_b_high", "card_c_high"}
            assert not (cards & other), f"{name} was served another household's card"

    def test_two_households_do_not_share_a_feed(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        a = client.get(f"/households/{DEMO}/decisions", headers=auth).json()
        b = client.get(
            f"/households/{household_id_for('monthly_thin')}/decisions", headers=auth
        ).json()

        assert a["summary"]["targeted_debt_id"] != b["summary"]["targeted_debt_id"]


class TestTheFileIsNoLongerTheSource:
    """`0024`'s point: the households come from Postgres.

    The AC as written was "a test asserts `main.py` does not read `decisions.json` at runtime",
    and it is **narrowed on purpose**: `/spend` still does, because its figures come from the whole
    transaction `History` and there is no `transactions` table to rebuild them from until ingest
    lands. That is ticket `0031`, and it is the whole of what is left. Narrowing it here rather
    than deleting it keeps the claim honest and keeps the day it becomes true visible.
    """

    def test_the_feed_does_not_touch_the_artifact(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """Proved by taking it away. A feed that still read the file would raise here; one that
        reads Postgres does not notice."""
        client.app.state.artifact = None

        response = client.get(f"/households/{DEMO}/decisions", headers=auth)

        assert response.status_code == 200
        assert len(response.json()["decisions"]) == 90

    def test_neither_does_the_explanation(self, client: TestClient, auth: dict[str, str]) -> None:
        client.app.state.artifact = None

        response = client.get(f"/households/{DEMO}/decisions/2026-03-02/explain", headers=auth)

        assert response.status_code == 200
        assert response.json()["narration"]

    def test_spend_is_the_one_that_still_does_and_says_so(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """Not an oversight — ticket 0031. This test exists so the exception cannot quietly
        become permanent: it fails the day `/spend` moves, which is the day to delete it."""
        client.app.state.artifact = None

        with pytest.raises(AttributeError):
            client.get("/spend", headers=auth)

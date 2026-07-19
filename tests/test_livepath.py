"""The live-assembly path — a write that changes the next decision (ticket 0056).

`0049`/`0050` proved the settings and attestation writes persist, audit, and read back. This proves
the thing they explicitly could not: that a written policy or attestation **changes a live
decision**. `backend/livepath.live_decision` reads `Repository.policy()` and `attested_for(repo)`
and threads both into the one canonical `walk()`/`assemble_snapshot()`, so:

- lowering `buffer_floor` turns a refusal into a sweep on the same day;
- attesting turns a `CARD_COVERAGE_INCOMPLETE` refusal into a sweep;
- an `UNMATCHED_PAYMENT` still refuses — a live attestation cannot clear it (the override holds);
- and the default (`attested=True`) leaves the shipped walk byte-for-byte unchanged, so
  `backend/replay.py`'s regression oracle is untouched.

The decision-changing proof is at the seam (a real `History`, a real Postgres household, the real
write path). The route (`GET /live-decision`) is guarded: a frozen demo household is `409`'d to
`/decisions`, and a linked household `409`s `no_linked_data` until the deferred `plaid_transactions`
→ `History` adapter lands — so those two guards are what the route tests assert.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend import livepath
from backend.attestation import attested_for, card_fingerprint
from backend.db.repository import repository
from backend.identity import stytch
from backend.identity.deps import get_verifier
from backend.precompute import (
    CARD_ID,
    DEMO_SPEC,
    SEED,
    SERVED_DAYS,
    WARMUP_DAYS,
    WINDOW_START,
    walk,
)
from engine.models import Action, CoverageState, ReasonCode
from sim.household import History, Txn, TxnKind, generate
from tests.conftest import requires_db

pytestmark = requires_db

HH = "hh_live"
_DAYS = WARMUP_DAYS + SERVED_DAYS

# Sane, internally consistent caps; only `buffer_floor` is varied across the policy tests.
_CAPS = {
    "max_sweep": Decimal("1600.00"),
    "max_weekly_sweep": Decimal("3200.00"),
    "min_days_between_sweeps": 7,
    "blackout_dates": [],
}
_LOW_FLOOR = Decimal("500.00")
_HIGH_FLOOR = Decimal("9999999.00")


def _history() -> History:
    """The demo household's realized life — the same `(spec, start, days, seed)` the seeder walks,
    so the decisions here are the ones the product actually serves."""
    return generate(DEMO_SPEC, start=WINDOW_START, days=_DAYS, seed=SEED)


def _sweep_day(history: History, buffer_floor: Decimal) -> dt.date:
    """The last day the household **sweeps** under `buffer_floor` (attested, the walk default).

    Found rather than hardcoded so the policy/attestation tests hinge on a genuine sweep: raising
    the floor or dropping the attestation on this day has something real to suppress.
    """
    from engine.models import UserPolicy

    policy = UserPolicy(buffer_floor=buffer_floor, **_CAPS)
    swept = [
        w.day
        for w in walk(history, DEMO_SPEC, WINDOW_START, _DAYS, policy=policy)
        if w.decision.action is Action.SWEEP
    ]
    assert swept, "the demo household never swept — the fixture cannot prove a suppression"
    return swept[-1]


def _with_unmatched(history: History) -> History:
    """`history` plus a recurring, card-shaped outflow to a card we cannot see — the exact signal
    `detect_unmatched_payments` fires on (KTD-7). Four monthly payments clear the 3-month
    recurrence bar, so the portfolio's coverage becomes `UNMATCHED_PAYMENT`."""
    extra = tuple(
        Txn(
            day=dt.date(2026, month, 15),
            amount=Decimal("-300.00"),
            label="CHASE CARD SVC",
            kind=TxnKind.CARD_PAYMENT,
            card_id=None,  # no card we can see — that is the whole point
        )
        for month in (1, 2, 3, 4)
    )
    txns = tuple(sorted(history.txns + extra, key=lambda t: t.day))
    return dataclasses.replace(history, txns=txns)


def _seed_household(db, app_engine, *, archetype: str | None, attest: bool) -> None:
    """One household with the demo card and an initial (low-floor) policy event; optionally
    attested. `archetype=None` is a *linked* household; a string is a frozen demo one.

    Raw rows go in through `db` (the owner role); the policy and attestation writes go through
    `app_engine` (the RLS-bound app role), the same split `test_attestation` uses — a write path
    exercised under the role it runs as in production."""
    with db.begin():
        db.execute(
            text("INSERT INTO households (id, archetype) VALUES (:h, :a)"),
            {"h": HH, "a": archetype},
        )
        db.execute(
            text(
                "INSERT INTO cards (id, household_id, apr, close_day_of_month, grace_days,"
                " statement_balance, statement_due_date, minimum_payment, unbilled_balance,"
                " next_close_date, behavior) VALUES (:c, :h, '0.2399', 20, 21, '1000.00',"
                " '2026-02-10', '25.00', '0.00', '2026-01-20', 'revolver')"
            ),
            {"c": CARD_ID, "h": HH},
        )
    with repository(app_engine, HH) as repo:
        repo.set_policy(buffer_floor=_LOW_FLOOR, **_CAPS)
        if attest:
            repo.add_attestation(card_fingerprint=card_fingerprint([CARD_ID]))


def _set_floor(app_engine: Engine, buffer_floor: Decimal) -> None:
    with repository(app_engine, HH) as repo:
        repo.set_policy(buffer_floor=buffer_floor, **_CAPS)


# --- the seam: a write changes the next decision ------------------------------------


class TestPolicyChangesTheDecision:
    def test_raising_the_buffer_floor_suppresses_the_sweep(self, db, app_engine) -> None:
        history = _history()
        today = _sweep_day(history, _LOW_FLOOR)
        _seed_household(db, app_engine, archetype=None, attest=True)

        _set_floor(app_engine, _LOW_FLOOR)
        with repository(app_engine, HH) as repo:
            low = livepath.live_decision(repo, history, today)
        assert low.decision.action is Action.SWEEP

        _set_floor(app_engine, _HIGH_FLOOR)
        with repository(app_engine, HH) as repo:
            high = livepath.live_decision(repo, history, today)
        assert high.decision.action is Action.REFUSE, (
            "a floor above the balance still swept — the live policy was not read"
        )
        assert high.decision != low.decision


class TestAttestationChangesTheDecision:
    def test_attesting_clears_the_coverage_refusal(self, db, app_engine) -> None:
        history = _history()
        today = _sweep_day(history, _LOW_FLOOR)
        _seed_household(db, app_engine, archetype=None, attest=False)

        with repository(app_engine, HH) as repo:
            unattested = livepath.live_decision(repo, history, today)
        assert unattested.snapshot.portfolio.coverage is CoverageState.UNATTESTED
        assert ReasonCode.CARD_COVERAGE_INCOMPLETE in unattested.decision.codes
        assert unattested.decision.action is Action.REFUSE

        with repository(app_engine, HH) as repo:
            repo.add_attestation(card_fingerprint=card_fingerprint([CARD_ID]))
        with repository(app_engine, HH) as repo:
            attested = livepath.live_decision(repo, history, today)
        assert attested.snapshot.portfolio.coverage is CoverageState.COMPLETE
        assert attested.decision.action is Action.SWEEP


class TestUnmatchedPaymentOverrideHolds:
    def test_a_live_attestation_cannot_clear_an_unmatched_payment(self, db, app_engine) -> None:
        history = _with_unmatched(_history())
        _seed_household(db, app_engine, archetype=None, attest=True)  # attested anyway

        with repository(app_engine, HH) as repo:
            assert attested_for(repo) is True  # the attestation IS present and current
            decided = livepath.live_decision(repo, history, history.end)

        assert decided.snapshot.portfolio.coverage is CoverageState.UNMATCHED_PAYMENT, (
            "an attestation overrode an unmatched card payment — the override did not hold"
        )
        assert decided.decision.action is Action.REFUSE


# --- the live spend surface: the comprehension half, built from live data ------------


class TestLiveSpendSurface:
    def test_a_linked_household_gets_a_live_spend_surface(self, db, app_engine) -> None:
        """The `/spend` comprehension surface, built live for a linked household — no seeded
        `decisions` row and no stored `spend_projection`, the two things
        `readpath.load_spend_surface` needs and a linked household lacks. Proves `live_spend`
        reuses walk → derive → assemble."""
        history = _history()
        _seed_household(db, app_engine, archetype=None, attest=True)

        with repository(app_engine, HH) as repo:
            surface = livepath.live_spend(repo, history)

        # The surface describes the last day of the linked history, and `assemble`'s staleness guard
        # (projection.as_of == snapshot.today) passed — both are `history.end` by construction.
        assert surface.as_of == history.end
        assert surface.projection.as_of == history.end
        # A card panel per card held, with per-card obligations that sum to the totals.
        assert len(surface.cards) >= 1
        assert surface.statement_total == sum(
            (c.statement_balance for c in surface.cards), Decimal("0")
        )


# --- the oracle: the default leaves the shipped walk unchanged ----------------------


class TestTheDefaultPreservesTheOracle:
    def test_default_and_explicit_true_are_identical(self) -> None:
        history = _history()
        default = [w.decision for w in walk(history, DEMO_SPEC, WINDOW_START, _DAYS)]
        explicit = [
            w.decision for w in walk(history, DEMO_SPEC, WINDOW_START, _DAYS, attested=True)
        ]
        assert default == explicit, "attested=True is the default; the seeder/replay path moved"

    def test_attested_false_actually_threads_through(self) -> None:
        history = _history()
        unattested = walk(history, DEMO_SPEC, WINDOW_START, _DAYS, attested=False)
        assert any(w.snapshot.portfolio.coverage is CoverageState.UNATTESTED for w in unattested), (
            "attested=False did not reach the coverage — the parameter is not wired"
        )


# --- the route: the two guards ------------------------------------------------------

OWNER_TOKEN, OWNER_STYTCH = "owner-token", "stytch-owner"


def _verifier():
    def verify(token: str):
        if token == OWNER_TOKEN:
            return OWNER_STYTCH, {}
        raise stytch.StytchVerificationError("stub")

    return verify


@pytest.fixture(autouse=True)
def secrets(monkeypatch: pytest.MonkeyPatch, app_engine: Engine) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    monkeypatch.setenv("DATABASE_URL", app_engine.url.render_as_string(hide_password=False))


def _add_owner(db) -> None:
    """The owner user + membership, so `authorize_household` yields a scoped repo for `HH`."""
    with db.begin():
        db.execute(
            text("INSERT INTO users (id, stytch_user_id) VALUES ('u-owner', :s)"),
            {"s": OWNER_STYTCH},
        )
        db.execute(
            text(
                "INSERT INTO household_members (household_id, user_id, role)"
                " VALUES (:h, 'u-owner', 'owner')"
            ),
            {"h": HH},
        )


@pytest.fixture
def client(app_engine) -> Iterator[TestClient]:
    from backend.main import app

    app.dependency_overrides[get_verifier] = _verifier
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_verifier, None)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {OWNER_TOKEN}"}


class TestTheRouteGuards:
    def test_a_frozen_demo_household_is_conflicted_to_the_served_feed(
        self, db, app_engine, client
    ) -> None:
        _seed_household(db, app_engine, archetype="demo_biweekly", attest=True)  # frozen
        _add_owner(db)
        resp = client.get(f"/households/{HH}/live-decision", headers=_auth())
        assert resp.status_code == 409
        assert resp.json()["error"] == "frozen_household"

    def test_a_linked_household_without_data_reports_no_linked_data(
        self, db, app_engine, client
    ) -> None:
        _seed_household(
            db, app_engine, archetype=None, attest=True
        )  # linked, but no History adapter yet
        _add_owner(db)
        resp = client.get(f"/households/{HH}/live-decision", headers=_auth())
        assert resp.status_code == 409
        assert resp.json()["error"] == "no_linked_data"

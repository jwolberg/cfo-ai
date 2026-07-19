"""Card-set attestation — the 0016 money-gate, closed in shadow (ticket 0050, U5).

Four layers, so the claim "the gate clears end to end" is not resting on any one of them:

- **the fingerprint** is a property of the card *set* (order/dup-independent, add/remove-sensitive);
- **the repository** appends attestations and reads the latest;
- **`attested_for`** returns True only while the attestation matches the *current* cards — a new
  card silently invalidates it;
- **`derive_portfolio` + `decide`** turn that bool into coverage and the money-gate: `UNATTESTED`
  refuses (`CARD_COVERAGE_INCOMPLETE`), attestation clears it to `COMPLETE`, and `UNMATCHED_PAYMENT`
  is never overridable.

Plus the route (`POST /attest`): owner-gated, appends, reports coverage.

(Shadow caveat: `readpath.py` serves frozen snapshots, so a *live* decision reflects the attestation
only once its snapshot is re-assembled — the plan's open live-assembly Prerequisite. What is proven
here is the write + invalidation + the gate logic.)
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend import precompute
from backend.attestation import attested_for, card_fingerprint, current_card_ids
from backend.db.repository import repository
from backend.identity import stytch
from backend.identity.deps import get_verifier
from engine.decide import decide
from engine.models import CoverageState, ReasonCode, UnmatchedPayment
from tests.conftest import requires_db

pytestmark = requires_db

OWNER_TOKEN, OWNER_STYTCH = "owner-token", "stytch-owner"
VIEWER_TOKEN, VIEWER_STYTCH = "viewer-token", "stytch-viewer"
OUTSIDER_TOKEN, OUTSIDER_STYTCH = "outsider-token", "stytch-outsider"
HH = "hh_attest"

_TOKENS = {
    OWNER_TOKEN: OWNER_STYTCH,
    VIEWER_TOKEN: VIEWER_STYTCH,
    OUTSIDER_TOKEN: OUTSIDER_STYTCH,
}


# --- the fingerprint (no DB) --------------------------------------------------------


class TestFingerprint:
    def test_it_is_order_and_duplicate_independent(self) -> None:
        assert card_fingerprint(["a", "b"]) == card_fingerprint(["b", "a", "b"])

    def test_it_changes_when_a_card_is_added(self) -> None:
        assert card_fingerprint(["a", "b"]) != card_fingerprint(["a", "b", "c"])

    def test_it_changes_when_a_card_is_removed(self) -> None:
        assert card_fingerprint(["a", "b"]) != card_fingerprint(["a"])

    def test_it_cannot_collide_by_concatenation(self) -> None:
        assert card_fingerprint(["ab", "c"]) != card_fingerprint(["a", "bc"])


# --- derive_portfolio coverage (the gate logic) -------------------------------------


class TestCoverageFromAttested:
    """`derive_portfolio` turns the `attested` bool into coverage. `detect_unmatched_payments` is
    patched so the test controls the unmatched signal without a full transaction history."""

    def _coverage(self, monkeypatch, *, attested: bool, unmatched) -> CoverageState:
        monkeypatch.setattr(
            precompute, "detect_unmatched_payments", lambda *a, **k: tuple(unmatched)
        )
        portfolio = precompute.derive_portfolio(
            history=None, cards=(), today=dt.date(2026, 3, 2), attested=attested
        )
        return portfolio.coverage

    def test_attested_and_no_unmatched_is_complete(self, monkeypatch) -> None:
        assert self._coverage(monkeypatch, attested=True, unmatched=[]) is CoverageState.COMPLETE

    def test_unattested_is_unattested(self, monkeypatch) -> None:
        assert self._coverage(monkeypatch, attested=False, unmatched=[]) is CoverageState.UNATTESTED

    def test_unmatched_overrides_attestation(self, monkeypatch) -> None:
        """KTD-7: a card-shaped outflow to a card we cannot see cannot be attested away."""
        um = UnmatchedPayment(
            merchant="Chase",
            typical_amount=Decimal("200.00"),
            day_of_month=15,
            months_observed=3,
        )
        assert (
            self._coverage(monkeypatch, attested=True, unmatched=[um])
            is CoverageState.UNMATCHED_PAYMENT
        )


# --- the money-gate in decide -------------------------------------------------------


def _snapshot_with_coverage(coverage: CoverageState):
    """A minimal snapshot whose only interesting property is portfolio coverage — enough to exercise
    the coverage refusal in `decide` without a full household."""
    from engine.models import (
        Account,
        AccountKind,
        CardPortfolio,
        ConnectionState,
        Snapshot,
        UserPolicy,
    )

    return Snapshot(
        today=dt.date(2026, 3, 2),
        accounts=(
            Account(
                account_id="chk",
                balance=Decimal("5000.00"),
                connection=ConnectionState.HEALTHY,
                balance_age_days=0,
                kind=AccountKind.CHECKING,
            ),
        ),
        funding_account_id="chk",
        events=(),
        pending=(),
        portfolio=CardPortfolio(cards=(), coverage=coverage, unmatched_card_payments=()),
        policy=UserPolicy(
            buffer_floor=Decimal("100.00"),
            max_sweep=Decimal("1000.00"),
            max_weekly_sweep=Decimal("2000.00"),
        ),
        daily_discretionary_high=Decimal("0.00"),
        income_variation=0.0,
        history_days=120,
    )


class TestTheMoneyGate:
    def test_unattested_is_refused_for_coverage(self) -> None:
        decision = decide(_snapshot_with_coverage(CoverageState.UNATTESTED))
        assert ReasonCode.CARD_COVERAGE_INCOMPLETE in decision.codes

    def test_complete_clears_the_coverage_refusal(self) -> None:
        decision = decide(_snapshot_with_coverage(CoverageState.COMPLETE))
        assert ReasonCode.CARD_COVERAGE_INCOMPLETE not in decision.codes


# --- the repository + attested_for (the write path + invalidation) ------------------


def _card(db, hid: str, cid: str) -> None:
    db.execute(
        text(
            "INSERT INTO cards (id, household_id, apr, close_day_of_month, grace_days,"
            " statement_balance, statement_due_date, minimum_payment, unbilled_balance,"
            " next_close_date, behavior) VALUES (:c, :h, '0.2399', 20, 21, '1000.00',"
            " '2026-02-10', '25.00', '0.00', '2026-01-20', 'revolver')"
        ),
        {"c": cid, "h": hid},
    )


class TestAttestedForAndInvalidation:
    def test_no_attestation_is_not_attested(self, db, app_engine) -> None:
        with db.begin():
            db.execute(
                text("INSERT INTO households (id, archetype) VALUES (:h, 'test')"), {"h": HH}
            )
            _card(db, HH, "card-1")
        with repository(app_engine, HH) as repo:
            assert attested_for(repo) is False

    def test_attesting_current_cards_makes_it_attested(self, db, app_engine) -> None:
        with db.begin():
            db.execute(
                text("INSERT INTO households (id, archetype) VALUES (:h, 'test')"), {"h": HH}
            )
            _card(db, HH, "card-1")
        with repository(app_engine, HH) as repo:
            repo.add_attestation(card_fingerprint=card_fingerprint(["card-1"]))
        with repository(app_engine, HH) as repo:
            assert attested_for(repo) is True

    def test_a_new_card_invalidates_the_attestation(self, db, app_engine) -> None:
        """The whole safety point (KTD-7): attest, then a card appears — coverage silently drops."""
        with db.begin():
            db.execute(
                text("INSERT INTO households (id, archetype) VALUES (:h, 'test')"), {"h": HH}
            )
            _card(db, HH, "card-1")
        with repository(app_engine, HH) as repo:
            repo.add_attestation(card_fingerprint=card_fingerprint(["card-1"]))
        with db.begin():
            _card(db, HH, "card-2")  # a new card appears
        with repository(app_engine, HH) as repo:
            assert attested_for(repo) is False, (
                "a stale attestation still covered a changed card set"
            )


# --- a LINKED household's cards are its Plaid credit accounts, not engine `cards` ----


def _credit_account(repo, plaid_account_id: str) -> None:
    repo.add_plaid_account(
        plaid_item_id="item-x",
        plaid_account_id=plaid_account_id,
        name="Card",
        official_name=None,
        type="credit",
        subtype="credit card",
        current_balance=Decimal("1400.00"),
        available_balance=None,
        iso_currency_code="USD",
    )


class TestLinkedCardAttestation:
    """The reconciliation: a linked household holds no engine `cards` row, so `current_card_ids`
    (and therefore attestation) reads its Plaid credit accounts instead — otherwise it could only
    ever attest an empty set and never clear coverage."""

    def test_current_card_ids_reads_plaid_credit_accounts_when_no_engine_cards(
        self, db, app_engine
    ) -> None:
        with db.begin():
            db.execute(text("INSERT INTO households (id, archetype) VALUES (:h, NULL)"), {"h": HH})
        with repository(app_engine, HH) as repo:
            _credit_account(repo, "acc_card_1")
        with repository(app_engine, HH) as repo:
            assert current_card_ids(repo) == ["acc_card_1"]

    def test_engine_cards_win_when_both_exist(self, db, app_engine) -> None:
        with db.begin():
            db.execute(
                text("INSERT INTO households (id, archetype) VALUES (:h, 'test')"), {"h": HH}
            )
            _card(db, HH, "card-1")
        with repository(app_engine, HH) as repo:
            _credit_account(repo, "acc_card_1")
        with repository(app_engine, HH) as repo:
            assert current_card_ids(repo) == ["card-1"]

    def test_attesting_a_linked_card_set_clears_and_a_new_card_invalidates(
        self, db, app_engine
    ) -> None:
        with db.begin():
            db.execute(text("INSERT INTO households (id, archetype) VALUES (:h, NULL)"), {"h": HH})
        with repository(app_engine, HH) as repo:
            _credit_account(repo, "acc_card_1")
            repo.add_attestation(card_fingerprint=card_fingerprint(current_card_ids(repo)))
        with repository(app_engine, HH) as repo:
            assert attested_for(repo) is True  # the linked card set is attested
            _credit_account(repo, "acc_card_2")  # a second linked card appears
        with repository(app_engine, HH) as repo:
            assert attested_for(repo) is False, "a new linked card did not drop coverage (KTD-7)"


# --- the route ----------------------------------------------------------------------


def _verifier():
    def verify(token: str):
        if token in _TOKENS:
            return _TOKENS[token], {}
        raise stytch.StytchVerificationError("stub")

    return verify


@pytest.fixture(autouse=True)
def secrets(monkeypatch: pytest.MonkeyPatch, app_engine: Engine) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    monkeypatch.setenv("DATABASE_URL", app_engine.url.render_as_string(hide_password=False))


@pytest.fixture
def route_seed(db):
    with db.begin():
        db.execute(text("INSERT INTO households (id, archetype) VALUES (:h, 'test')"), {"h": HH})
        _card(db, HH, "card-1")
        for uid, sid in (("u-owner", OWNER_STYTCH), ("u-viewer", VIEWER_STYTCH)):
            db.execute(
                text("INSERT INTO users (id, stytch_user_id) VALUES (:u, :s)"), {"u": uid, "s": sid}
            )
        db.execute(
            text(
                "INSERT INTO household_members (household_id, user_id, role)"
                " VALUES (:h, 'u-owner', 'owner'), (:h, 'u-viewer', 'viewer')"
            ),
            {"h": HH},
        )
    return db


@pytest.fixture
def client(route_seed, app_engine) -> Iterator[TestClient]:
    from backend.main import app

    app.dependency_overrides[get_verifier] = _verifier
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_verifier, None)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestTheAttestRoute:
    def test_an_owner_attests_and_it_lands(self, client, route_seed, app_engine) -> None:
        resp = client.post(f"/households/{HH}/attest", headers=_auth(OWNER_TOKEN))
        assert resp.status_code == 200
        assert resp.json()["attested"] is True
        with repository(app_engine, HH) as repo:
            att = repo.current_attestation()
        assert att is not None and att["attested_by"] == "u-owner"

    def test_a_viewer_cannot_attest(self, client, route_seed) -> None:
        assert (
            client.post(f"/households/{HH}/attest", headers=_auth(VIEWER_TOKEN)).status_code == 403
        )

    def test_a_non_member_cannot_attest(self, client, route_seed) -> None:
        assert (
            client.post(f"/households/{HH}/attest", headers=_auth(OUTSIDER_TOKEN)).status_code
            == 403
        )

    def test_no_session_is_401(self, client, route_seed) -> None:
        assert client.post(f"/households/{HH}/attest").status_code == 401

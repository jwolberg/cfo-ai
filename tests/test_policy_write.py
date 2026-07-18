"""PATCH /households/{id}/policy — the product's first user-driven write (ticket 0049).

The append-only settings path: a validated guardrail change lands as a new `policy_events` row,
`Repository.policy()` reads the latest, prior events survive, and a loosening change is flagged.
Owner-gated (the demo `viewer` cannot write) and membership-scoped like every other route.

The write is proven end to end through the route; the append/latest-read mechanics are also checked
at the repository level. (No live decision reflects the change yet — the demo uses a hardcoded
policy and `readpath.py` serves frozen snapshots; the live-assembly path is the open Prerequisite.)
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.db.repository import repository
from backend.identity import stytch
from backend.identity.deps import get_verifier
from tests.conftest import requires_db

pytestmark = requires_db

OWNER_TOKEN, OWNER_STYTCH = "owner-token", "stytch-owner"
VIEWER_TOKEN, VIEWER_STYTCH = "viewer-token", "stytch-viewer"
OUTSIDER_TOKEN, OUTSIDER_STYTCH = "outsider-token", "stytch-outsider"
HH = "hh_policy"

_TOKENS = {
    OWNER_TOKEN: OWNER_STYTCH,
    VIEWER_TOKEN: VIEWER_STYTCH,
    OUTSIDER_TOKEN: OUTSIDER_STYTCH,
}

_BASE = {
    "buffer_floor": "800.00",
    "max_sweep": "1600.00",
    "max_weekly_sweep": "3200.00",
    "min_days_between_sweeps": 7,
    "blackout_dates": [],
}


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
def seeded(db):
    """One household with an owner and a viewer, plus an initial policy event."""
    with db.begin():
        db.execute(text("INSERT INTO households (id, archetype) VALUES (:h, 'test')"), {"h": HH})
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
        db.execute(
            text(
                "INSERT INTO policy_events (id, household_id, buffer_floor, max_sweep,"
                " max_weekly_sweep, min_days_between_sweeps) VALUES"
                " ('pe-init', :h, '800.00', '1600.00', '3200.00', 7)"
            ),
            {"h": HH},
        )
    return db


@pytest.fixture
def client(seeded, app_engine) -> Iterator[TestClient]:
    from backend.main import app

    app.dependency_overrides[get_verifier] = _verifier
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_verifier, None)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _events(db):
    with db.begin():
        return (
            db.execute(
                text(
                    "SELECT buffer_floor, max_sweep, max_weekly_sweep, min_days_between_sweeps,"
                    " loosened, changed_by FROM policy_events WHERE household_id = :h ORDER BY seq"
                ),
                {"h": HH},
            )
            .mappings()
            .all()
        )


class TestTheWritePath:
    def test_a_change_persists_and_reads_back(self, client, app_engine, seeded) -> None:
        body = {**_BASE, "buffer_floor": "500.00"}
        resp = client.patch(f"/households/{HH}/policy", headers=_auth(OWNER_TOKEN), json=body)
        assert resp.status_code == 200
        assert resp.json()["buffer_floor"] == "500.00"

        with repository(app_engine, HH) as repo:
            assert repo.policy()["buffer_floor"] == Decimal("500.00")

    def test_the_change_is_appended_not_mutated(self, client, seeded) -> None:
        before = len(_events(seeded))
        client.patch(
            f"/households/{HH}/policy",
            headers=_auth(OWNER_TOKEN),
            json={**_BASE, "buffer_floor": "500.00"},
        )
        after = _events(seeded)
        assert len(after) == before + 1, "the write replaced a row instead of appending"
        # The initial event's values survive — history is non-destructive.
        assert after[0]["buffer_floor"] == Decimal("800.00")
        assert after[-1]["buffer_floor"] == Decimal("500.00")

    def test_the_actor_is_recorded(self, client, seeded) -> None:
        client.patch(
            f"/households/{HH}/policy",
            headers=_auth(OWNER_TOKEN),
            json={**_BASE, "min_days_between_sweeps": 10},
        )
        assert _events(seeded)[-1]["changed_by"] == "u-owner"


class TestReadPolicy:
    def test_a_member_reads_the_current_policy(self, client, seeded) -> None:
        resp = client.get(f"/households/{HH}/policy", headers=_auth(OWNER_TOKEN))
        assert resp.status_code == 200
        assert resp.json()["buffer_floor"] == "800.00"

    def test_it_reflects_a_write(self, client, seeded) -> None:
        client.patch(
            f"/households/{HH}/policy",
            headers=_auth(OWNER_TOKEN),
            json={**_BASE, "buffer_floor": "500.00"},
        )
        resp = client.get(f"/households/{HH}/policy", headers=_auth(OWNER_TOKEN))
        assert resp.json()["buffer_floor"] == "500.00"

    def test_a_viewer_may_read(self, client, seeded) -> None:
        """Reads are allowed for any member; only writes need owner."""
        resp = client.get(f"/households/{HH}/policy", headers=_auth(VIEWER_TOKEN))
        assert resp.status_code == 200

    def test_a_non_member_is_403(self, client, seeded) -> None:
        assert (
            client.get(f"/households/{HH}/policy", headers=_auth(OUTSIDER_TOKEN)).status_code == 403
        )


class TestLooseningIsFlagged:
    def test_a_lower_floor_is_flagged_loosening(self, client, seeded) -> None:
        client.patch(
            f"/households/{HH}/policy",
            headers=_auth(OWNER_TOKEN),
            json={**_BASE, "buffer_floor": "500.00"},  # 800 -> 500 loosens
        )
        assert _events(seeded)[-1]["loosened"] is True

    def test_a_higher_cap_is_flagged_loosening(self, client, seeded) -> None:
        client.patch(
            f"/households/{HH}/policy",
            headers=_auth(OWNER_TOKEN),
            json={**_BASE, "max_sweep": "2000.00", "max_weekly_sweep": "4000.00"},
        )
        assert _events(seeded)[-1]["loosened"] is True

    def test_a_tightening_change_is_not_flagged(self, client, seeded) -> None:
        client.patch(
            f"/households/{HH}/policy",
            headers=_auth(OWNER_TOKEN),
            json={**_BASE, "buffer_floor": "900.00"},  # 800 -> 900 tightens
        )
        assert _events(seeded)[-1]["loosened"] is False


class TestValidationRejectsWithoutWriting:
    @pytest.mark.parametrize(
        "override",
        [
            {"buffer_floor": "-1.00"},
            {"max_sweep": "0.50"},  # below MIN_SWEEP ($1)
            {"max_weekly_sweep": "100.00"},  # below max_sweep
            {"min_days_between_sweeps": -1},
        ],
    )
    def test_an_invalid_change_is_422_and_appends_nothing(self, client, seeded, override) -> None:
        before = len(_events(seeded))
        resp = client.patch(
            f"/households/{HH}/policy", headers=_auth(OWNER_TOKEN), json={**_BASE, **override}
        )
        assert resp.status_code == 422
        assert len(_events(seeded)) == before, "a rejected change still landed in the audit trail"


class TestAuthorization:
    def test_a_viewer_cannot_write(self, client, seeded) -> None:
        resp = client.patch(f"/households/{HH}/policy", headers=_auth(VIEWER_TOKEN), json=_BASE)
        assert resp.status_code == 403

    def test_a_non_member_cannot_write(self, client, seeded) -> None:
        resp = client.patch(f"/households/{HH}/policy", headers=_auth(OUTSIDER_TOKEN), json=_BASE)
        assert resp.status_code == 403

    def test_no_session_is_401(self, client, seeded) -> None:
        assert client.patch(f"/households/{HH}/policy", json=_BASE).status_code == 401

"""The webhook doorbell: what it verifies, what it drops, and what it never does.

The doorbell is a **public** endpoint — Plaid calls it and does not carry our API key — so its only
authentication is the `Plaid-Verification` signature. These tests forge, replay, and malform that
signature, and assert the door stays shut; then they let a genuine one through and assert exactly
one enqueue and no Plaid call in the request path.

Every test drives the real app through `TestClient`, with the Plaid client and the Cloud Tasks
enqueuer replaced by FastAPI dependency overrides — so nothing here touches Plaid or a queue, but
the verification, the persistence, and the dedup are the real code.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterator

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from jwt.algorithms import ECAlgorithm
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.auth import API_KEY_ENV
from backend.plaid import webhook
from backend.plaid.deps import get_enqueuer, get_plaid_client
from tests.conftest import requires_db

pytestmark = requires_db

KEY = "test-key-not-a-real-one"
KID = "test-kid"


# --- a real EC keypair, so the signature check is real ------------------------------------------

_PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
_JWK = json.loads(ECAlgorithm.to_jwk(_PRIVATE_KEY.public_key()))


def _sign(body: bytes, *, iat: dt.datetime | None = None, kid: str = KID) -> str:
    """A valid Plaid-Verification JWT over `body`: signs the body's SHA-256, as Plaid does."""
    issued = iat or dt.datetime.now(tz=dt.timezone.utc)
    claims = {
        "iat": int(issued.timestamp()),
        "request_body_sha256": hashlib.sha256(body).hexdigest(),
    }
    return jwt.encode(claims, _PRIVATE_KEY, algorithm="ES256", headers={"kid": kid})


# --- fakes for the injected dependencies --------------------------------------------------------


class _Key:
    def __init__(self, jwk: dict) -> None:
        self._jwk = jwk

    def to_dict(self) -> dict:
        return self._jwk


class _KeyResponse:
    def __init__(self, jwk: dict) -> None:
        self.key = _Key(jwk)


class FakePlaidClient:
    """Returns our test JWK for verification and asserts the doorbell never syncs inline."""

    def __init__(self) -> None:
        self.key_lookups: list[str] = []

    def webhook_verification_key_get(self, request):
        self.key_lookups.append(request.key_id)
        return _KeyResponse(_JWK)

    def transactions_sync(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("the doorbell must not call Plaid in the request path")


# --- fixtures -----------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def secrets(monkeypatch: pytest.MonkeyPatch, app_engine: Engine) -> None:
    monkeypatch.setenv(API_KEY_ENV, KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    monkeypatch.setenv("DATABASE_URL", app_engine.url.render_as_string(hide_password=False))


@pytest.fixture
def enqueued() -> list[str]:
    return []


@pytest.fixture
def fake_client() -> FakePlaidClient:
    return FakePlaidClient()


@pytest.fixture
def client(db, fake_client: FakePlaidClient, enqueued: list[str]) -> Iterator[TestClient]:
    """The real app, with the Plaid client and the enqueuer replaced. `db` empties the tables."""
    from backend.main import app

    webhook._key_cache.clear()  # keys are cached by kid; a fresh keypair per session must not stick
    app.dependency_overrides[get_plaid_client] = lambda: fake_client
    app.dependency_overrides[get_enqueuer] = lambda: lambda item_id: enqueued.append(item_id) or "t"
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _webhook_body(item_id: str = "item-1", code: str = "SYNC_UPDATES_AVAILABLE") -> bytes:
    # Compact separators so the bytes we sign are the bytes we send.
    return json.dumps(
        {"item_id": item_id, "webhook_type": "TRANSACTIONS", "webhook_code": code},
        separators=(",", ":"),
    ).encode()


def _rows(db) -> int:
    with db.begin():
        return db.execute(text("SELECT count(*) FROM plaid_webhooks")).scalar()


# --- the door stays shut ------------------------------------------------------------------------


class TestForgedAndMissingSignatures:
    def test_no_signature_is_rejected_before_any_persist_or_enqueue(
        self, client: TestClient, enqueued: list[str], db
    ) -> None:
        body = _webhook_body()
        response = client.post("/plaid/webhook", content=body)  # no Plaid-Verification header
        assert response.status_code == 401
        assert _rows(db) == 0, "an unsigned payload reached the table"
        assert enqueued == [], "an unsigned payload triggered an enqueue"

    def test_a_forged_signature_is_rejected(
        self, client: TestClient, enqueued: list[str], db
    ) -> None:
        """A JWT signed by a key that is not the one Plaid vouches for."""
        body = _webhook_body()
        attacker_key = ec.generate_private_key(ec.SECP256R1())
        claims = {
            "iat": int(dt.datetime.now(tz=dt.timezone.utc).timestamp()),
            "request_body_sha256": hashlib.sha256(body).hexdigest(),
        }
        forged = jwt.encode(claims, attacker_key, algorithm="ES256", headers={"kid": KID})
        response = client.post(
            "/plaid/webhook", content=body, headers={"Plaid-Verification": forged}
        )
        assert response.status_code == 401
        assert _rows(db) == 0
        assert enqueued == []

    def test_a_valid_signature_over_a_different_body_is_rejected(
        self, client: TestClient, enqueued: list[str], db
    ) -> None:
        """The body-hash check: a genuine JWT replayed against a tampered payload."""
        signed_body = _webhook_body(item_id="item-1")
        token = _sign(signed_body)
        tampered = _webhook_body(item_id="item-attacker")
        response = client.post(
            "/plaid/webhook", content=tampered, headers={"Plaid-Verification": token}
        )
        assert response.status_code == 401
        assert _rows(db) == 0
        assert enqueued == []

    def test_a_stale_signature_is_rejected(
        self, client: TestClient, enqueued: list[str], db
    ) -> None:
        body = _webhook_body()
        old = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(minutes=10)
        response = client.post(
            "/plaid/webhook", content=body, headers={"Plaid-Verification": _sign(body, iat=old)}
        )
        assert response.status_code == 401
        assert _rows(db) == 0
        assert enqueued == []


# --- a genuine webhook gets through, exactly once -----------------------------------------------


class TestAGenuineWebhook:
    def test_it_persists_and_enqueues_without_calling_plaid(
        self, client: TestClient, enqueued: list[str], fake_client: FakePlaidClient, db
    ) -> None:
        body = _webhook_body(item_id="item-42")
        response = client.post(
            "/plaid/webhook", content=body, headers={"Plaid-Verification": _sign(body)}
        )
        assert response.status_code == 200
        assert _rows(db) == 1, "the raw payload was not persisted"
        assert enqueued == ["item-42"], "the sync was not enqueued exactly once"
        # The only Plaid call is the key fetch for verification — never a sync in the request path.
        assert fake_client.key_lookups == [KID]

    def test_a_redelivered_webhook_is_deduped_and_does_not_re_enqueue(
        self, client: TestClient, enqueued: list[str], db
    ) -> None:
        """Same item + code + (null) cursor twice. The second conflicts on the dedup key, inserts
        nothing, and enqueues nothing — the resumable sync makes a missed redelivery harmless.
        """
        body = _webhook_body(item_id="item-7")
        first = client.post(
            "/plaid/webhook", content=body, headers={"Plaid-Verification": _sign(body)}
        )
        second = client.post(
            "/plaid/webhook", content=body, headers={"Plaid-Verification": _sign(body)}
        )
        assert (first.status_code, second.status_code) == (200, 200)
        assert _rows(db) == 1, "the redelivery was stored as a second row"
        assert enqueued == ["item-7"], "the redelivery re-enqueued a sync"

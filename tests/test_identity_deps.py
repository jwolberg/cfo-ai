"""The identity edge — token verification, JIT provisioning, and household authorization.

Ticket 0047 (U2). Two halves:

- `TestStytchVerify` exercises the **real decode path** against a generated RSA keypair (no network,
  no Stytch project) — a valid token verifies, and every tampering is refused. This is what makes
  the "rejected before any DB touch" claim real rather than asserted: the refusals here never reach
  a database.
- `TestCurrentUser` / `TestAuthorizeHousehold` mount the dependencies on a throwaway app with a
  **stubbed verifier** and a real Postgres, and prove the gate: JIT provisioning is idempotent, a
  non-member is `403`'d, and writes require `owner`.

The real-Stytch-sandbox end-to-end gate is U7 (`tests/test_identity_sandbox.py`); a green stubbed
suite is explicitly not accepted as evidence for that.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Annotated

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import text

from backend.db.repository import Repository
from backend.db.session import StytchSecretWouldLeak, assert_stytch_secret_safe_at_rest
from backend.identity import stytch
from backend.identity.deps import (
    User,
    authorize_household,
    authorize_household_owner,
    current_user,
    get_verifier,
)
from tests.conftest import requires_db

PROJECT_ID = "project-test-0000"
KID = "test-kid-1"


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
    return pem, jwk


def _token(pem: bytes, *, sub="user-test-1", aud=PROJECT_ID, iss=None, iat=None, exp=None, kid=KID):
    now = dt.datetime.now(tz=dt.timezone.utc)
    payload = {
        "sub": sub,
        "aud": aud,
        "iss": iss if iss is not None else f"stytch.com/{PROJECT_ID}",
        "iat": iat if iat is not None else now,
        "exp": exp if exp is not None else now + dt.timedelta(hours=1),
    }
    return jwt.encode(payload, pem, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def stytch_keys(monkeypatch):
    """A generated signing key wired into `stytch` in place of a real JWKS fetch."""
    pem, jwk = _keypair()
    monkeypatch.setenv("STYTCH_PROJECT_ID", PROJECT_ID)
    monkeypatch.setattr(stytch, "_fetch_jwks", lambda: {"keys": [jwk]})
    stytch._reset_key_cache()
    yield pem
    stytch._reset_key_cache()


class TestStytchVerify:
    """The decode path — a valid token verifies, every tampering refused before any DB touch."""

    def test_a_valid_token_verifies_and_returns_the_subject(self, stytch_keys) -> None:
        stytch_user_id, claims = stytch.verify(_token(stytch_keys))
        assert stytch_user_id == "user-test-1"
        assert claims["iss"] == f"stytch.com/{PROJECT_ID}"

    def test_an_expired_token_is_refused(self, stytch_keys) -> None:
        past = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(hours=2)
        token = _token(stytch_keys, iat=past, exp=past + dt.timedelta(minutes=5))
        with pytest.raises(stytch.StytchVerificationError):
            stytch.verify(token)

    def test_a_wrong_audience_is_refused(self, stytch_keys) -> None:
        with pytest.raises(stytch.StytchVerificationError):
            stytch.verify(_token(stytch_keys, aud="project-test-someone-else"))

    def test_a_wrong_issuer_is_refused(self, stytch_keys) -> None:
        with pytest.raises(stytch.StytchVerificationError):
            stytch.verify(_token(stytch_keys, iss="stytch.com/project-test-imposter"))

    def test_a_tampered_signature_is_refused(self, stytch_keys) -> None:
        token = _token(stytch_keys)
        tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
        with pytest.raises(stytch.StytchVerificationError):
            stytch.verify(tampered)

    def test_a_wrong_algorithm_is_refused_before_any_key_fetch(self, monkeypatch) -> None:
        """An HS256 token must be refused at the alg check, never verified against an RSA key — the
        alg-confusion downgrade. This fails before the JWKS is even consulted."""
        monkeypatch.setenv("STYTCH_PROJECT_ID", PROJECT_ID)

        def _boom():
            raise AssertionError("the JWKS was fetched for a wrong-alg token")

        monkeypatch.setattr(stytch, "_fetch_jwks", _boom)
        hs = jwt.encode(
            {"sub": "x"},
            "shared-secret-at-least-32-bytes-long!",
            algorithm="HS256",
            headers={"kid": KID},
        )
        with pytest.raises(stytch.StytchVerificationError, match="alg"):
            stytch.verify(hs)

    def test_a_non_jwt_is_refused(self, stytch_keys) -> None:
        with pytest.raises(stytch.StytchVerificationError):
            stytch.verify("not-a-jwt")

    def test_an_unknown_kid_is_refused(self, stytch_keys) -> None:
        with pytest.raises(stytch.StytchVerificationError, match="kid"):
            stytch.verify(_token(stytch_keys, kid="some-other-kid"))

    def test_the_jwks_cache_evicts_on_the_ttl(self, monkeypatch) -> None:
        """KTD-4: a session key is verified repeatedly, so a stale cache entry must be refetched
        within the TTL — a rotated or revoked key stops being trusted in a bounded window, not only
        on process restart. Unlike the never-evict Plaid pattern."""
        _, jwk = _keypair()
        monkeypatch.setenv("STYTCH_PROJECT_ID", PROJECT_ID)
        fetches = {"n": 0}

        def _counting_fetch():
            fetches["n"] += 1
            return {"keys": [jwk]}

        monkeypatch.setattr(stytch, "_fetch_jwks", _counting_fetch)
        stytch._reset_key_cache()

        stytch._signing_key_for(KID)
        stytch._signing_key_for(KID)
        assert fetches["n"] == 1, "a warm cache within the TTL refetched"

        # Age the cached entry past the TTL — the next lookup must refetch.
        stytch._key_cache[KID].fetched_at -= stytch._JWKS_TTL + dt.timedelta(seconds=1)
        stytch._signing_key_for(KID)
        assert fetches["n"] == 2, "a stale cache entry was not evicted on the TTL"
        stytch._reset_key_cache()


class TestStytchSecretGuard:
    """`assert_stytch_secret_safe_at_rest` — the tripwire on the first real (non-test) Stytch
    project, the identity analogue of the Plaid token guard. Trigger is real users, not money-on."""

    def test_test_env_boots(self, monkeypatch) -> None:
        monkeypatch.setenv("STYTCH_ENV", "test")
        assert_stytch_secret_safe_at_rest()  # does not raise

    def test_unset_defaults_to_test_and_boots(self, monkeypatch) -> None:
        monkeypatch.delenv("STYTCH_ENV", raising=False)
        assert_stytch_secret_safe_at_rest()  # does not raise

    def test_live_is_refused_while_the_secret_is_not_managed(self, monkeypatch) -> None:
        monkeypatch.setenv("STYTCH_ENV", "live")
        with pytest.raises(StytchSecretWouldLeak, match="real users"):
            assert_stytch_secret_safe_at_rest()

    def test_the_refusal_does_not_echo_the_secret(self, monkeypatch) -> None:
        """A guard that prints the credential it is protecting is the leak it guards against."""
        monkeypatch.setenv("STYTCH_ENV", "live")
        monkeypatch.setenv("STYTCH_SECRET", "secret-live-do-not-print-me")
        with pytest.raises(StytchSecretWouldLeak) as e:
            assert_stytch_secret_safe_at_rest()
        assert "secret-live-do-not-print-me" not in str(e.value)


# --- the dependencies, under a stubbed verifier and a real Postgres ---------------------


def _app(engine, verifier):
    """A throwaway app hanging routes on the real dependencies, with the verifier stubbed."""
    app = FastAPI()
    app.state.db = engine

    @app.get("/me")
    def me(user: Annotated[User, Depends(current_user)]):
        return {"id": user.id, "stytch_user_id": user.stytch_user_id}

    @app.get("/households/{household_id}/probe")
    def probe(repo: Annotated[Repository, Depends(authorize_household)]):
        return {"household": repo.household_id}

    @app.post("/households/{household_id}/write")
    def write(repo: Annotated[Repository, Depends(authorize_household_owner)]):
        return {"household": repo.household_id}

    app.dependency_overrides[get_verifier] = lambda: verifier
    return app


def _stub(mapping: dict[str, str]):
    """A verifier: token -> stytch_user_id, else refuse (as a real bad token would)."""

    def verify(token: str):
        if token not in mapping:
            raise stytch.StytchVerificationError("stub: unknown token")
        return mapping[token], {}

    return verify


@requires_db
class TestCurrentUser:
    def test_a_missing_bearer_is_401(self, app_engine) -> None:
        client = TestClient(_app(app_engine, _stub({})))
        assert client.get("/me").status_code == 401

    def test_an_invalid_token_is_401_and_provisions_nothing(self, db, app_engine) -> None:
        client = TestClient(_app(app_engine, _stub({"good": "s-1"})))
        resp = client.get("/me", headers={"Authorization": "Bearer bad"})
        assert resp.status_code == 401
        with db.begin():
            n = db.execute(text("SELECT count(*) FROM users")).scalar()
        assert n == 0, "an unverified token provisioned a user"

    def test_a_valid_token_jit_provisions_once(self, db, app_engine) -> None:
        client = TestClient(_app(app_engine, _stub({"tok": "s-new"})))
        first = client.get("/me", headers={"Authorization": "Bearer tok"})
        second = client.get("/me", headers={"Authorization": "Bearer tok"})
        assert first.status_code == second.status_code == 200
        assert first.json()["id"] == second.json()["id"], "a second login minted a second user"
        with db.begin():
            n = db.execute(
                text("SELECT count(*) FROM users WHERE stytch_user_id = 's-new'")
            ).scalar()
        assert n == 1


@requires_db
class TestAuthorizeHousehold:
    def _seed(self, db, *, user="u-alice", stytch_id="s-alice", role="owner", household="hh1"):
        with db.begin():
            db.execute(
                text("INSERT INTO households (id, archetype) VALUES (:h, 'test')"), {"h": household}
            )
            db.execute(
                text("INSERT INTO users (id, stytch_user_id) VALUES (:u, :s)"),
                {"u": user, "s": stytch_id},
            )
            db.execute(
                text(
                    "INSERT INTO household_members (household_id, user_id, role)"
                    " VALUES (:h, :u, :r)"
                ),
                {"h": household, "u": user, "r": role},
            )

    def test_a_member_gets_a_scoped_repository(self, db, app_engine) -> None:
        self._seed(db)
        client = TestClient(_app(app_engine, _stub({"tok": "s-alice"})))
        resp = client.get("/households/hh1/probe", headers={"Authorization": "Bearer tok"})
        assert resp.status_code == 200
        assert resp.json()["household"] == "hh1"

    def test_a_valid_but_non_member_is_403(self, db, app_engine) -> None:
        """The refusal this whole rung exists to make pass: a verified session naming a household it
        does not belong to is refused even though the id is correct."""
        self._seed(db)  # alice is a member of hh1 only
        with db.begin():
            db.execute(text("INSERT INTO households (id, archetype) VALUES ('hh2', 'test')"))
        client = TestClient(_app(app_engine, _stub({"tok": "s-alice"})))
        resp = client.get("/households/hh2/probe", headers={"Authorization": "Bearer tok"})
        assert resp.status_code == 403

    def test_a_viewer_may_read_but_not_write(self, db, app_engine) -> None:
        """KTD-10: the public demo `viewer` reads every demo household and writes none."""
        self._seed(db, user="u-view", stytch_id="s-view", role="viewer")
        client = TestClient(_app(app_engine, _stub({"tok": "s-view"})))
        assert (
            client.get("/households/hh1/probe", headers={"Authorization": "Bearer tok"}).status_code
            == 200
        )
        assert (
            client.post(
                "/households/hh1/write", headers={"Authorization": "Bearer tok"}
            ).status_code
            == 403
        )

    def test_an_owner_may_write(self, db, app_engine) -> None:
        self._seed(db, role="owner")
        client = TestClient(_app(app_engine, _stub({"tok": "s-alice"})))
        assert (
            client.post(
                "/households/hh1/write", headers={"Authorization": "Bearer tok"}
            ).status_code
            == 200
        )

    def test_a_non_member_cannot_write_either(self, db, app_engine) -> None:
        self._seed(db)
        with db.begin():
            db.execute(text("INSERT INTO households (id, archetype) VALUES ('hh2', 'test')"))
        client = TestClient(_app(app_engine, _stub({"tok": "s-alice"})))
        assert (
            client.post(
                "/households/hh2/write", headers={"Authorization": "Bearer tok"}
            ).status_code
            == 403
        )

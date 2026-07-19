"""A dev-only auth proxy: keeps a local browser session alive despite the 5-minute Stytch JWT.

The problem it solves (local dogfooding only): a Stytch `session_jwt` expires in ~5 minutes, and no
refresh mechanism is built (the "+ refresh" half of KTD-4 was never implemented). The Expo web app
bakes one token at bundle time, so a browser session dies after 5 minutes with 401s. This proxy
sits between the web app and the real API: it mints a fresh dogfood session for the configured user,
caches it, re-mints before it expires, and injects `Authorization: Bearer <fresh jwt>` on every
forwarded request. The browser's own token is ignored.

NOT a deploy path and NOT a security mechanism — it hands full owner access to whoever can reach it,
which is exactly why it only ever runs on localhost against the local API. Production auth needs a
real web sign-in + token refresh (tickets 0055 + the unbuilt KTD-4 refresh), not this.

    STYTCH_PROJECT_ID=... STYTCH_SECRET="$(security find-generic-password -s cfo-ai-stytch-secret -w)" \
      UPSTREAM=http://localhost:8000 DOGFOOD_EMAIL=dogfood@example.com \
      .venv/bin/uvicorn scripts.dev_auth_proxy:app --host 127.0.0.1 --port 8010
"""

from __future__ import annotations

import os
import time

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

UPSTREAM = os.environ.get("UPSTREAM", "http://localhost:8000")
EMAIL = os.environ.get("DOGFOOD_EMAIL", "dogfood@example.com")
_PASSWORD = "dogfood-correct-horse-battery-staple-9x!"  # matches scripts/mint_session.py

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8081", "http://localhost:19006"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_client = httpx.AsyncClient(base_url=UPSTREAM, timeout=30.0)
_token: dict[str, float | str] = {"jwt": "", "exp": 0.0}


def _fresh_jwt() -> str:
    """A dogfood session_jwt, re-minted when the cached one is within 60s of its 5-minute expiry."""
    now = time.time()
    if _token["jwt"] and float(_token["exp"]) - now > 60:
        return str(_token["jwt"])
    from stytch import Client

    stytch = Client(
        project_id=os.environ["STYTCH_PROJECT_ID"],
        secret=os.environ["STYTCH_SECRET"],
        environment="test",
        suppress_warnings=True,
    )
    try:
        resp = stytch.passwords.create(email=EMAIL, password=_PASSWORD, session_duration_minutes=180)
    except Exception:  # noqa: BLE001 — existing user: authenticate instead of create
        resp = stytch.passwords.authenticate(
            email=EMAIL, password=_PASSWORD, session_duration_minutes=180
        )
    _token["jwt"] = resp.session_jwt
    _token["exp"] = now + 300  # a session_jwt lives 5 minutes; re-mint before then
    return resp.session_jwt


@app.api_route("/{path:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
async def proxy(path: str, request: Request) -> Response:
    body = await request.body()
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "authorization", "content-length", "origin")
    }
    headers["Authorization"] = f"Bearer {_fresh_jwt()}"
    upstream = await _client.request(
        request.method, f"/{path}", content=body, headers=headers, params=request.query_params
    )
    passthrough = {
        k: v for k, v in upstream.headers.items() if k.lower() not in ("content-length", "content-encoding")
    }
    return Response(content=upstream.content, status_code=upstream.status_code, headers=passthrough)

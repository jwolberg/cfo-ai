"""The decision-history service.

One demo household, one precomputed artifact, no database and no write path. The service
loads `backend/data/decisions.json` once at startup, validates it, and serves it from
memory — see `docs/decisions/0002-generated-json-artifact-over-database.md`.

## Money crosses the wire as a string

Every dollar amount is serialized as `"400.00"`, not `400.0`. JSON has one number type and
it is a double; a cent that round-trips through a double is no longer the cent the engine
decided on. The same discipline `money()` enforces inside the process
(`engine/models.py` refuses floats outright) has to survive the boundary, so the client
receives text and formats it, and no float ever touches a dollar amount in either direction.

## Failure lands at startup, not per-request

A missing or malformed artifact — or a missing API key — raises before the first request is
served, and the process exits. A service that comes up holding bad data and answers with
wrong numbers is worse than one that never comes up: Cloud Run reports the second, and
nobody notices the first.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from backend import artifact as art
from backend.auth import expected_key, require_api_key

# The Expo web target runs in a browser, on a different origin from the API — so without
# CORS the whole `expo start --web` verification path fails while native targets work fine.
# Native (simulator/device) requests are not subject to CORS at all, which is exactly why
# this is easy to forget until the browser is the thing you are testing in.
DEFAULT_ORIGINS = (
    "http://localhost:8081",  # Expo dev server (Metro)
    "http://localhost:19006",  # expo start --web
)


def allowed_origins() -> list[str]:
    configured = os.environ.get("RESFI_ALLOWED_ORIGINS", "")
    if not configured:
        return list(DEFAULT_ORIGINS)
    return [origin.strip() for origin in configured.split(",") if origin.strip()]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load and validate everything the service needs, or refuse to start."""
    expected_key()  # before the artifact: a public endpoint is the worse failure
    app.state.artifact = art.load()
    yield


app = FastAPI(
    title="Resfi decision history",
    summary="The demo household's decisions, and why the engine made them.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def artifact_of(request: Request) -> art.Artifact:
    return request.app.state.artifact


# The `Annotated` form of FastAPI's dependency injection, rather than `= Depends(...)` in the
# signature's defaults: same wiring, but it does not put a function call in an argument
# default, which is a real footgun everywhere else in Python and which linters rightly flag.
ArtifactDep = Annotated[art.Artifact, Depends(artifact_of)]


def usd(amount: Decimal | None) -> str | None:
    """A dollar amount, as text. See the module docstring on why this is not a number."""
    return None if amount is None else str(amount)


def decision_json(record: art.DayRecord) -> dict[str, Any]:
    """One day, as the client sees it.

    The reason *codes* travel with the decision. They are the stable fact — the sentence is
    one rendering of it (`engine/explain.py`) — and the client is given both rather than
    being left to infer the outcome from prose.
    """
    decision = record.decision
    return {
        "date": record.day.isoformat(),
        "action": decision.action.value,
        "amount": usd(decision.amount),
        "target_debt_id": decision.target_debt_id,
        "projected_low_balance": usd(decision.projected_low_balance),
        "reason_codes": [code.value for code in decision.codes],
        # "Paid off" is a REFUSE carrying NO_DEBT, never a third action. Derived in one place
        # (`DayRecord.paid_off`) so the UI cannot invent a different rule for it.
        "paid_off": record.paid_off,
        "checking_balance": usd(record.checking_balance),
        "savings_balance": usd(record.savings_balance),
        "buffer_floor": usd(record.buffer_floor),
        "debt_balance": usd(record.debt_balance),
        "debt_id": record.debt_id,
    }


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Cloud Run's probe. Deliberately unauthenticated, and deliberately says nothing.

    If the artifact were missing the process would not be here to answer.
    """
    return {"status": "ok"}


@app.get("/decisions", dependencies=[Depends(require_api_key)])
async def decisions(artifact: ArtifactDep) -> dict[str, Any]:
    """The served window, newest first — the order the feed reads in."""
    return {
        "window": {
            "start": artifact.window_start.isoformat(),
            "end": artifact.window_end.isoformat(),
            # The last served day is the demo's "today". It is a fixed calendar date, not the
            # wall clock, and the client and the assistant both resolve relative dates
            # ("last Tuesday") against it.
            "today": artifact.window_end.isoformat(),
        },
        "decisions": [decision_json(record) for record in reversed(artifact.days)],
    }

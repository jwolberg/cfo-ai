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
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend import artifact as art
from backend.auth import expected_key, require_api_key
from engine.explain import explain, render

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

    Both the reason *codes* and their rendered sentences travel with the decision. The code
    is the stable fact; the sentence is one rendering of it. The client gets both rather than
    being left to infer the outcome from prose — and, just as importantly, the backend never
    writes a sentence of its own. All product copy lives in `engine/explain.py` and nowhere
    else, which is what lets a copy edit be a copy edit rather than a financial change.
    """
    decision = record.decision
    return {
        "date": record.day.isoformat(),
        "action": decision.action.value,
        "amount": usd(decision.amount),
        "target_debt_id": decision.target_debt_id,
        "projected_low_balance": usd(decision.projected_low_balance),
        "reason_codes": [code.value for code in decision.codes],
        "reasons": [
            {"code": reason.code.value, "text": render(reason)} for reason in decision.reasons
        ],
        # "Paid off" is a REFUSE carrying NO_DEBT, never a third action. Derived in one place
        # (`DayRecord.paid_off`) so the UI cannot invent a different rule for it.
        "paid_off": record.paid_off,
        "checking_balance": usd(record.checking_balance),
        "savings_balance": usd(record.savings_balance),
        "buffer_floor": usd(record.buffer_floor),
        "debt_balance": usd(record.debt_balance),
        "debt_id": record.debt_id,
    }


def summary_json(summary: art.Summary) -> dict[str, Any]:
    """The dashboard's headline stats (R3).

    `interest_avoided_total` sums the `INTEREST_AVOIDED` reasons the engine itself chose to
    emit. It is not recomputed here — `engine/interest.py` declines to make that claim when
    it cannot stand behind it, and summing only the claims it *did* make inherits that
    discipline instead of reimplementing it.
    """
    return {
        "interest_avoided_total": usd(summary.interest_avoided_total),
        "total_swept": usd(summary.total_swept),
        "current_buffer": usd(summary.current_buffer),
        "targeted_debt_id": summary.targeted_debt_id,
        "targeted_debt_balance": usd(summary.targeted_debt_balance),
        "sweep_count": summary.sweep_count,
        "refuse_count": summary.refuse_count,
        "paid_off": summary.paid_off,
    }


def no_record(day: str) -> JSONResponse:
    """The one answer for every date we cannot speak to.

    Before the window, after it, or inside the warm-up runway that was never served — all
    the same response, deliberately. The user asked whether we did something on a day, and
    the honest answer is that we have nothing on record. Which of the three reasons it was
    is our business, not theirs, and distinguishing them would leak the shape of the demo.

    A 404, not a 500: there is nothing wrong with the service, and nothing wrong with the
    question.
    """
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={
            "error": "no_record",
            "date": day,
            "message": "We don't have a decision on record for that day.",
        },
    )


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
        "summary": summary_json(artifact.summary),
        "decisions": [decision_json(record) for record in reversed(artifact.days)],
    }


@app.get("/decisions/{day}/explain", dependencies=[Depends(require_api_key)])
async def explain_decision(day: str, artifact: ArtifactDep) -> Any:
    """Why the engine did what it did on `day`, in plain language. No LLM in this path.

    This is the whole of R4. `engine/explain.py` already turns the decision's reason codes
    into sentences, deterministically and with a test asserting every code has copy — so
    tapping a decision costs one in-memory lookup and zero network calls. The assistant
    (U4) is for the *follow-up* question, not for reading back what the engine decided;
    routing base narration through a model would mean the most-viewed text in the product
    was the one thing that could hallucinate.

    An unparseable date gets the same "no record" answer as a real date we have nothing for.
    A 422 on the malformed one would tell a caller which of the two they sent, and there is
    no reason for the two to be distinguishable here.
    """
    try:
        when = date.fromisoformat(day)
    except ValueError:
        return no_record(day)

    record = artifact.by_day(when)
    if record is None:
        return no_record(day)

    return {
        **decision_json(record),
        "narration": list(explain(record.decision)),
    }

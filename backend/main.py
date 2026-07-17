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
from pydantic import BaseModel, Field

from backend import artifact as art
from backend import assistant
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
    # Built once, at startup, so a deploy without the Anthropic secret fails here rather
    # than the first time a user opens the modal and asks a question.
    app.state.assistant = assistant.build_client()
    app.state.rate_cap = assistant.RateCap()
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
        # Every card, with its own rate and where that rate came from — ticket 0030. A portfolio at
        # 27.99% and 17.99% does not have "a debt", and `debt_balance` alone answered a question
        # nobody asked while hiding the one that matters.
        #
        # `apr_source` crosses the wire with the rate because the client cannot tell a 23% estimate
        # from a reported 23% otherwise, and `0028` decided we would act on the estimate and never
        # claim from it. A UI that renders a guessed rate as a fact is that decision undone at the
        # last possible moment.
        "debts": [
            {
                "debt_id": d.debt_id,
                "balance": usd(d.balance),
                "apr": None if d.apr is None else str(d.apr),
                "apr_source": d.apr_source.value,
            }
            for d in record.debts
        ],
        # The portfolio total. Derived in one place (`DayRecord.debt_balance`) so the client and the
        # summary cannot disagree about what "your debt" is.
        "debt_balance": usd(record.debt_balance),
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
        # The denominator and numerator for "how far down is it", both portfolio totals. Sent as
        # figures, never as a percentage: the UI can divide, and a percentage computed here would
        # be a second place the number lives.
        #
        # `current_debt_balance` rather than pairing `starting -> targeted`, which reads correctly
        # only while a household has one card. On a portfolio it compares a total against a single
        # card and renders progress that did not happen (ticket 0030).
        "starting_debt_balance": usd(summary.starting_debt_balance),
        "current_debt_balance": usd(summary.current_debt_balance),
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


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Cloud Run's probe. Deliberately unauthenticated, and deliberately says nothing.

    If the artifact were missing the process would not be here to answer.

    Named `/health`, not `/healthz`: Google's frontend reserves `/healthz` on `*.run.app` and
    answers it itself with a Google 404, so a route by that name is defined here and never
    reached in production. It fails in a way that reads like a broken deploy — the 404 body is
    Google's HTML, carries no `server: Google Frontend` header, and produces no request log —
    while every other path, including ones this app has never heard of, arrives normally.
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


@app.get("/spend", dependencies=[Depends(require_api_key)])
async def spend(artifact: ArtifactDep) -> dict[str, Any]:
    """What the household spends, and what their card is about to take.

    **Comprehension, not a decision.** Nothing served here feeds the engine. The rolling 30-day
    series is the exact structure that will eventually replace `daily_discretionary_high` in the
    forecast — rendered a release *before* it is trusted with a decision, so it earns its way in
    having already been looked at by real households. Swapping the forecast onto it today would
    *loosen* the reserve, and loosening needs a measured breach rate we cannot yet produce.

    The two obligations are reported separately because they fall due a **month apart**. A single
    "what you owe" figure hides precisely the thing the user needs to see: what is already
    committed, and what is quietly forming behind it.
    """
    s = artifact.spend

    return {
        "as_of": artifact.window_end.isoformat(),
        "this_cycle": {
            # Already closed. Legally due, inside the horizon, and reserved.
            "statement": {
                "amount": usd(s.statement_balance),
                "due": s.statement_due.isoformat(),
                "reserved": True,
            },
            # Charged since. Not yet due — this is next month's bill, forming now, and it is
            # the number that makes the card an engine input at all.
            "unbilled": {
                "amount": usd(s.unbilled_balance),
                "due": s.unbilled_due.isoformat(),
                "reserved": False,
            },
            # The line that stops the reserve looking arbitrary: "we're holding back $X for
            # this."
            "held_back": usd(s.reserved),
        },
        "last_cycle": {
            "charged": usd(s.charged_last_cycle),
            "paid": usd(s.paid_last_cycle),
            # Positive means the card GREW. A sweep will not catch that up — the spending is
            # the thing to change, and the product should say so rather than stay quiet.
            "grew_by": usd(s.card_grew_by),
        },
        "normal": {
            # Every overlapping 30-day total in the trailing window. The strip chart, and the
            # answer to "what does a bad month actually look like for me".
            "rolling_30d_cash": [usd(v) for v in s.rolling_30d_cash],
            "rolling_30d_card": [usd(v) for v in s.rolling_30d_card],
            "worst_30d_cash": usd(s.worst_30d_cash),
            "worst_30d_card": usd(s.worst_30d_card),
        },
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


class Turn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str


class AssistantRequest(BaseModel):
    """The conversation, resent by the client each turn.

    There is no server-side session store — the modal holds the conversation and sends it
    back. That is what keeps "the artifact is the only state this service has" true, and it
    is also why the assistant must re-fetch a decision every turn rather than trusting what
    it said earlier: the history is the client's word, not the engine's.
    """

    message: str = Field(min_length=1, max_length=2000)
    history: list[Turn] = Field(default_factory=list, max_length=40)


@app.post("/assistant/message", dependencies=[Depends(require_api_key)])
async def assistant_message(body: AssistantRequest, request: Request) -> dict[str, Any]:
    """A follow-up question about a decision. The only path in the service that costs money.

    The reply is whatever survives the verification guard (`backend/assistant.py`) — a
    narrated answer, an honest "no record", or an availability apology. The `outcome` field
    says which, so the client can render a caught hallucination and a timeout differently
    even when their copy reads alike.
    """
    reply = assistant.answer(
        artifact=artifact_of(request),
        history=[turn.model_dump() for turn in body.history],
        message=body.message,
        client=request.app.state.assistant,
        rate_cap=request.app.state.rate_cap,
    )

    return {"reply": reply.text, "outcome": reply.outcome.value}

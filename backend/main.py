"""The decision-history service.

**Households come from Postgres, scoped by `household_id` twice over** — once by `0021`'s
repository and again by row-level security (`architecture.md` [4]). The decision feed and the
explanations are rebuilt from the `decisions` and `snapshots` rows the seeder wrote; see
`backend/readpath.py`. ADR-0004 supersedes ADR-0002, and ticket `0024` is where the service
stopped being a file reader.

## Nothing here reads a file

`/spend` was the exception until ticket `0031`, and it is not one any more: the committed artifact
is a test fixture and this process never opens it. Every route is scoped, and every figure comes
from a row.

`/spend` is the one route whose data has two sources, and the split is deliberate. Each card's
statement, unbilled balance and reserve are derived **live** from the frozen `Snapshot` — the same
per-card terms `untouchable()` sums. What only the full transaction `History` can answer (the
rolling 30-day series; what each card took last cycle) is a **stored projection**, because there is
no `transactions` table until ingest lands (`architecture.md` [3.1]) — and ingest deletes it. See
`backend/spend.py`.

## Money crosses the wire as a string

Every dollar amount is serialized as `"400.00"`, not `400.0`. JSON has one number type and
it is a double; a cent that round-trips through a double is no longer the cent the engine
decided on. The same discipline `money()` enforces inside the process
(`engine/models.py` refuses floats outright) has to survive the boundary, so the client
receives text and formats it, and no float ever touches a dollar amount in either direction.

## Failure lands at startup, not per-request

A missing API key, an unreachable or unmigrated database, a role that would bypass RLS — every one
of them raises before the first request is served, and the process exits. A service that comes up
holding bad data and answers with wrong numbers is worse than one that never comes up: Cloud Run
reports the second, and nobody notices the first (ADR-0004 [3.2]).

The database check is not a ping. It runs `assert_rls_binds()`, which refuses to start under a role
that is `rolsuper` or has `rolbypassrls` — because Neon's default role has the latter, and under it
a household-scoped query returns **every** household while the IDOR suite stays green. A service
that can reach the database is not the same as a service whose scoping works.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Path, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from backend import assistant, readpath

# The wire shapes, imported as types and nothing else. `backend.artifact` is where they are
# defined; the *file* that module reads and writes is a test fixture (`0031`) and this process
# never opens it. Importing the names rather than the module is what keeps that visible.
from backend.artifact import DayRecord, Summary
from backend.auth import expected_key, require_api_key
from backend.db.repository import repository
from backend.db.session import assert_rls_binds, make_engine
from backend.db.snapshots import PostgresSnapshotStore
from backend.spend import CardObligations, SpendProjection
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
    expected_key()  # before anything else: a public endpoint is the worse failure

    # Reachable, migrated, and — the part a ping would miss — connected as a role that RLS
    # actually binds for. See the module docstring.
    app.state.db = make_engine()
    with app.state.db.connect() as conn:
        assert_rls_binds(conn)
        _assert_migrated(conn)

    # Built once, at startup, so a deploy without the Anthropic secret fails here rather
    # than the first time a user opens the modal and asks a question.
    app.state.assistant = assistant.build_client()
    app.state.rate_cap = assistant.RateCap()
    yield
    app.state.db.dispose()


def _assert_migrated(conn: Connection) -> None:
    """The schema exists. Checked at startup, because the alternative is finding out per-request.

    Deliberately not an Alembic revision comparison: this asserts the tables the read path actually
    reads, which is the claim that matters. A service pinned to the right revision against a
    database that lost a table is still a service that cannot answer.

    `spend_projections` joined the list with ticket `0031`, and it is the reason this is a list of
    what is *read* rather than a list of what exists: it is a table the read path depends on and
    that ingest will one day drop. When that happens this line moves, and the check stays true.
    """
    for table in ("households", "decisions", "snapshots", "spend_projections"):
        try:
            conn.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
        except SQLAlchemyError as exc:
            raise RuntimeError(
                f"the database has no readable `{table}` — run `alembic upgrade head`, and see "
                f"docs/runbooks/local-development.md"
            ) from exc


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


# The household id travels in the path and is bound to both scoping layers in one place
# (`repository()`), which is what stops them disagreeing. Every route below that touches a
# household's data goes through it — there is no other way to reach a row.
HouseholdId = Annotated[str, Path(description="Which household. `GET /households` lists them.")]


def _window(request: Request, household_id: str) -> readpath.ServedWindow:
    """One household's served window, scoped, in one transaction.

    Read fresh per request rather than cached at startup: a cache would be a second copy of a
    number the database already holds, and `architecture.md` [1.2] cut Redis because no
    cache-shaped access pattern exists. One decision per household per day is not one.
    """
    with repository(request.app.state.db, household_id) as repo:
        return readpath.load_window(repo, PostgresSnapshotStore(repo.conn))


def usd(amount: Decimal | None) -> str | None:
    """A dollar amount, as text. See the module docstring on why this is not a number."""
    return None if amount is None else str(amount)


def decision_json(record: DayRecord) -> dict[str, Any]:
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


def summary_json(summary: Summary) -> dict[str, Any]:
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


def no_household(household_id: str) -> JSONResponse:
    """A household we have nothing on record for.

    A 404, and specifically **not** an empty 200: a `decisions: []` would say this household exists
    and the engine decided nothing for it, which is a different claim and a false one. It is also
    not a 500 — nothing is wrong with the service, and nothing is wrong with the question.

    It does not distinguish "never existed" from "exists with no decisions", for the same reason
    `no_record` does not distinguish its three cases: which one it was is our business.
    """
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={
            "error": "no_household",
            "household_id": household_id,
            "message": "We don't have anything on record for that household.",
        },
    )


def no_spend_projection(household_id: str) -> JSONResponse:
    """The household is real, its feed renders, and its spend surface was never derived.

    **A 404 for the surface, not for the household**, and a distinct error code from
    `no_household` — this is a seeding fault (`0031`: a household seeded before the projection
    existed, or by something that skipped it), and reporting it as "no such household" would send
    whoever debugs it looking for rows that are right there.

    Not a 500: the service is fine and the question was fair. Not an empty 200 with zeros: a
    Spending screen full of zeros says this household spends nothing, which is the false-but-
    plausible answer this codebase keeps refusing to give.
    """
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={
            "error": "no_spend_projection",
            "household_id": household_id,
            "message": "We don't have a spending picture for that household yet.",
        },
    )


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Cloud Run's probe. Deliberately unauthenticated, and deliberately says nothing.

    Named `/health`, not `/healthz`: Google's frontend reserves `/healthz` on `*.run.app` and
    answers it itself with a Google 404, so a route by that name is defined here and never
    reached in production. It fails in a way that reads like a broken deploy — the 404 body is
    Google's HTML, carries no `server: Google Frontend` header, and produces no request log —
    while every other path, including ones this app has never heard of, arrives normally.
    """
    return {"status": "ok"}


@app.get("/households", dependencies=[Depends(require_api_key)])
async def households(request: Request) -> dict[str, Any]:
    """Every household the demo can be switched to.

    **The one query that is not household-scoped**, because it is the query you ask before you have
    a household to scope to. It returns ids and labels and nothing else: no balances, no decisions,
    nothing an id alone should buy. Everything past this point goes through the repository and RLS.

    Any API key may list, and select, any household. That is a demo posture and `USERS.md` §1 says
    so plainly — these households are synthetic and have no owner to authenticate as. Clerk arrives
    with Plaid, and the IDOR suite is real in the meantime, which is worth exactly as much as the
    identity feeding it.
    """
    with request.app.state.db.connect() as conn:
        found = readpath.list_households(conn)

    return {"households": [{"id": h.id, "archetype": h.archetype, "label": h.label} for h in found]}


@app.get("/households/{household_id}/decisions", dependencies=[Depends(require_api_key)])
async def decisions(request: Request, household_id: HouseholdId) -> Any:
    """The served window, newest first — the order the feed reads in."""
    try:
        window = _window(request, household_id)
    except readpath.NoSuchHousehold:
        return no_household(household_id)

    return {
        "window": {
            "start": window.window_start.isoformat(),
            "end": window.window_end.isoformat(),
            # The last served day is the demo's "today". It is a fixed calendar date, not the
            # wall clock, and the client and the assistant both resolve relative dates
            # ("last Tuesday") against it.
            "today": window.window_end.isoformat(),
        },
        "summary": summary_json(window.summary),
        "decisions": [decision_json(record) for record in reversed(window.days)],
    }


@app.get("/households/{household_id}/spend", dependencies=[Depends(require_api_key)])
async def spend(request: Request, household_id: HouseholdId) -> Any:
    """What the household spends, and what their cards are about to take.

    **Every card, and this household's.** Ticket `0031` — the route that used to read the committed
    file and serve `portfolio.cards[0]` as "your card" to whichever household asked. It is scoped
    through `0021`'s repository like every other route now, and it names each card.

    **The two halves come from different places, and that is the design.** Each card's statement,
    unbilled balance and reserve are derived live from the frozen `Snapshot` — `untouchable()`'s own
    per-card terms, so the figures add up to the number the engine actually withheld rather than
    resembling it. What only the transaction `History` can answer (the rolling 30-day series; what
    each card took last cycle) is a stored projection, because there is no `transactions` table
    until ingest lands (`architecture.md` [3.1]) — and ingest deletes it. `backend/spend.py` carries
    the argument for why storing the first half too would be the worse choice.

    **Comprehension, not a decision.** Nothing served here feeds the engine. The rolling 30-day
    series is the exact structure that will eventually replace `daily_discretionary_high` in the
    forecast — rendered a release *before* it is trusted with a decision, so it earns its way in
    having already been looked at by real households. Swapping the forecast onto it today would
    *loosen* the reserve, and loosening needs a measured breach rate we cannot yet produce.

    **There is no single due date, and no single "what you owe".** The two obligations are reported
    separately because they fall due a **month apart**, and a combined figure hides precisely what
    the user needs to see: what is already committed, and what is quietly forming behind it. With
    three cards that argument gets stronger, not weaker — the cards do not close together — so the
    totals below are sums of money and never of dates.
    """
    try:
        with repository(request.app.state.db, household_id) as repo:
            surface = readpath.load_spend_surface(repo, PostgresSnapshotStore(repo.conn))
    except readpath.NoSuchHousehold:
        return no_household(household_id)
    except readpath.NoSpendProjection:
        return no_spend_projection(household_id)

    return {
        "as_of": surface.as_of.isoformat(),
        "cards": [_card_spend_json(c, surface.projection) for c in surface.cards],
        # Sums, and only of money. The reserve total is the portfolio-level fact `untouchable()`
        # returns; the per-card `held_back` figures above are the terms it is the sum of.
        "totals": {
            "statement": usd(surface.statement_total),
            "unbilled": usd(surface.unbilled_total),
            "held_back": usd(surface.held_back_total),
        },
        "normal": {
            # Every overlapping 30-day total in the trailing window. The strip chart, and the
            # answer to "what does a bad month actually look like for me". Cash is a household
            # fact; the card series is every card's charges, which is what a bad month means for
            # a household holding three of them.
            "rolling_30d_cash": [usd(v) for v in surface.projection.rolling_30d_cash],
            "rolling_30d_card": [usd(v) for v in surface.projection.rolling_30d_card],
            "worst_30d_cash": usd(surface.projection.worst_30d_cash),
            "worst_30d_card": usd(surface.projection.worst_30d_card),
        },
    }


def _card_spend_json(card: CardObligations, projection: SpendProjection) -> dict[str, Any]:
    """One card's panel: what it owes, what it is forming, and what it took last cycle.

    `last_cycle` is `null` when the projection has no totals for this card rather than zeros —
    "nothing was charged" and "we have no transactions for this card" are different claims, and
    only one of them is safe to print next to "your card grew by $0.00".
    """
    totals = projection.totals_for(card.card_id)

    return {
        "card_id": card.card_id,
        "this_cycle": {
            # Already closed. Legally due, inside the horizon, and reserved.
            "statement": {
                "amount": usd(card.statement_balance),
                "due": card.statement_due.isoformat(),
                "reserved": True,
            },
            # Charged since. Not yet due — this is next month's bill, forming now, and it is
            # the number that makes the card an engine input at all.
            "unbilled": {
                "amount": usd(card.unbilled_balance),
                "due": card.unbilled_due.isoformat(),
                "reserved": False,
            },
            # The line that stops the reserve looking arbitrary: "we're holding back $X for this."
            # This card's own `obligation_in_horizon`, not a share of the total.
            "held_back": usd(card.held_back),
        },
        "last_cycle": None
        if totals is None
        else {
            "charged": usd(totals.charged_last_cycle),
            "paid": usd(totals.paid_last_cycle),
            # Positive means the card GREW. A sweep will not catch that up — the spending is
            # the thing to change, and the product should say so rather than stay quiet.
            "grew_by": usd(totals.grew_by),
        },
    }


@app.get(
    "/households/{household_id}/decisions/{day}/explain",
    dependencies=[Depends(require_api_key)],
)
async def explain_decision(request: Request, household_id: HouseholdId, day: str) -> Any:
    """Why the engine did what it did on `day`, in plain language. No LLM in this path.

    This is the whole of R4. `engine/explain.py` already turns the decision's reason codes
    into sentences, deterministically and with a test asserting every code has copy — so
    tapping a decision costs one scoped query and zero model calls. The assistant
    (U4) is for the *follow-up* question, not for reading back what the engine decided;
    routing base narration through a model would mean the most-viewed text in the product
    was the one thing that could hallucinate.

    This is also `architecture.md` [3.3]'s first promise arriving: "why did you move $220 that
    Tuesday" is answered from the frozen snapshot of that Tuesday, so it stays true after Plaid
    has rewritten the transactions underneath it.

    An unparseable date gets the same "no record" answer as a real date we have nothing for.
    A 422 on the malformed one would tell a caller which of the two they sent, and there is
    no reason for the two to be distinguishable here.
    """
    try:
        when = date.fromisoformat(day)
    except ValueError:
        return no_record(day)

    with repository(request.app.state.db, household_id) as repo:
        record = readpath.decision_on(repo, PostgresSnapshotStore(repo.conn), when)

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
    back. That is why the assistant must re-fetch a decision every turn rather than trusting what
    it said earlier: the history is the client's word, not the engine's.

    `household_id` is in the **body** rather than the path because it is an input to the answer,
    not a sub-resource of it: this route reads a household's decisions and posts nothing to it.
    """

    household_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=2000)
    history: list[Turn] = Field(default_factory=list, max_length=40)


@app.post("/assistant/message", dependencies=[Depends(require_api_key)])
async def assistant_message(body: AssistantRequest, request: Request) -> Any:
    """A follow-up question about a decision. The only path in the service that costs money.

    The reply is whatever survives the verification guard (`backend/assistant.py`) — a
    narrated answer, an honest "no record", or an availability apology. The `outcome` field
    says which, so the client can render a caught hallucination and a timeout differently
    even when their copy reads alike.

    The window is loaded **scoped**, and handed to the model as the only decisions that exist.
    That is the same guarantee the guard already gives one level down — the model may not assert a
    figure it did not fetch — arriving one level up: it cannot fetch another household's figure to
    assert in the first place.
    """
    try:
        window = _window(request, body.household_id)
    except readpath.NoSuchHousehold:
        return no_household(body.household_id)

    reply = assistant.answer(
        artifact=window,
        history=[turn.model_dump() for turn in body.history],
        message=body.message,
        client=request.app.state.assistant,
        rate_cap=request.app.state.rate_cap,
    )

    return {"reply": reply.text, "outcome": reply.outcome.value}

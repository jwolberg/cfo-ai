"""The explain assistant: Claude narrates, the engine decides, and a guard checks the prose.

The LLM is never in the decision path. It cannot compute a sweep, price interest, or form a
reason — those already happened, deterministically, at build time (`engine/`). Its only job
is to answer a follow-up question *about* decisions that already exist, by retrieving them
through two structured tools and describing what it found.

## Why a guard, and not a good system prompt

A system prompt asking a model not to invent numbers is a request. This module's contract is
that the product **cannot** state a financial claim it can't trace to a `Decision` — and a
request is not a contract. So every dollar figure, outcome, and reason code in the model's
final text is checked against the tool results *from this turn* before the user sees a word
of it. Anything that doesn't verify replaces the whole response with "no record."

That makes the guard, not the prompt, the actual test surface for the no-fabrication
guarantee — and it is why `tests/test_assistant.py` spends most of its length trying to
sneak a false claim past it rather than checking that the happy path works.

## Claims are bound to a date, not pooled

The subtle failure this is built against: the model fetches two real decisions and then
recombines them — quoting decision A's $400 sweep against decision B's date. Every number
in that sentence is real. A presence-only check ("is $400 somewhere in the tool results?")
waves it through, and the user is told something false out of entirely true parts.

So facts are keyed by `(date, field)`. A sentence that names a date may only carry figures
belonging to *that* decision. See `verify()`.

## Three outcomes that look similar and are not the same thing

- **No record** — a tool was asked about a day we have nothing for. An honest answer, not a
  failure. The user asked; we don't know; we said so.
- **Guard rejection** — the model tried to state something it could not support. A caught
  hallucination. Rare, and worth knowing about.
- **Unavailable** — Anthropic timed out, errored, or the tool loop ran away. An availability
  problem that says nothing about truth.

Two of them render similar text to the user. They are kept distinct in `Outcome` so a later
logging pass can tell "the model tried to fabricate something" apart from "there was nothing
to answer" — a distinction that is invisible in the copy and matters enormously in a metric.
"""

from __future__ import annotations

import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from backend.artifact import Artifact, DayRecord
from engine.explain import explain
from engine.models import Action, ReasonCode

MODEL = "claude-opus-4-8"

# Non-streaming is fine at this scale: one household, a handful of decisions per turn, and a
# reply of a few sentences. Thinking stays adaptive so the model can reason about which day
# the user means before reaching for a tool; effort is capped at medium because this is
# narration over a tiny structured dataset, not a research problem.
MAX_TOKENS = 4096
EFFORT = "medium"

# Bounds on a single turn. The loop cap stops a model that has decided to page through the
# whole window one day at a time; the timeout stops a turn from hanging the modal open.
MAX_TOOL_ROUNDS = 5
API_TIMEOUT_SECONDS = 30.0

# The rate cap. The API key is recoverable from the client bundle (see `backend/auth.py`), so
# key leakage is the expected steady state rather than an edge case — and this is the thing
# that actually bounds the Anthropic bill when it happens. An in-memory counter is only
# authoritative because the deployment pins `--max-instances=1`: with two instances, each
# holds its own counter and the real ceiling silently doubles. That flag and this constant
# are a pair; changing one without the other breaks the guarantee.
MAX_TURNS_PER_MINUTE = 10
MAX_TURNS_PER_DAY = 300


class Outcome(str, Enum):
    ANSWERED = "answered"
    NO_RECORD = "no_record"
    GUARD_REJECTED = "guard_rejected"
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"


NO_RECORD_REPLY = (
    "I don't have that on record. I can only speak to the decisions this household's "
    "engine actually made — if I can't trace an answer to one, I'd rather say so than "
    "guess."
)

UNAVAILABLE_REPLY = (
    "I couldn't reach the assistant just then. The decisions and their explanations are "
    "all still here — try asking again."
)

RATE_LIMITED_REPLY = "That's a lot of questions at once. Give it a minute and try again."


# --- the facts a turn is allowed to assert ------------------------------------------


# The money fields of a decision the assistant may quote. Anything not in here cannot be
# stated as a dollar figure at all, because the guard has nothing to check it against.
def money_facts(record: DayRecord) -> dict[str, Decimal]:
    """Every dollar figure this decision supports, keyed by field name."""
    facts: dict[str, Decimal] = {
        "amount": record.decision.amount,
        "checking_balance": record.checking_balance,
        "savings_balance": record.savings_balance,
        "buffer_floor": record.buffer_floor,
        "debt_balance": record.debt_balance,
    }

    if record.decision.projected_low_balance is not None:
        facts["projected_low_balance"] = record.decision.projected_low_balance

    # The reasons carry the rest — the projection's low/buffer/reserved, the interest
    # avoided, the idle savings. Pulled from the engine's own params rather than recomputed.
    for reason in record.decision.reasons:
        for key, value in reason.params.items():
            if isinstance(value, Decimal):
                facts[f"{reason.code.value}.{key}"] = value

    return facts


@dataclass
class Facts:
    """What the backend itself fetched this turn — the only things the model may assert."""

    by_date: dict[date, DayRecord] = field(default_factory=dict)

    def record(self, record: DayRecord) -> None:
        self.by_date[record.day] = record

    def amounts_for(self, day: date) -> set[Decimal]:
        record = self.by_date.get(day)
        return set(money_facts(record).values()) if record else set()

    def all_amounts(self) -> set[Decimal]:
        return {amount for day in self.by_date for amount in self.amounts_for(day)}

    @property
    def empty(self) -> bool:
        return not self.by_date


# --- tools ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_decision",
        "description": (
            "Look up what the engine decided for this household on one specific day, and "
            "why. Call this whenever the user asks about a particular day — including when "
            "they name it indirectly ('last Tuesday', 'the day before that'). Returns the "
            "outcome, the amount, the plain-language reasons, and the balances the decision "
            "was made against. If there is no decision on record for that day, it says so; "
            "that is a real answer, not an error."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "The day to look up, as YYYY-MM-DD.",
                }
            },
            "required": ["date"],
        },
    },
    {
        "name": "list_decisions",
        "description": (
            "List the decisions across a date range, optionally filtered to one reason code. "
            "Call this when the user asks about a pattern rather than a single day — 'how "
            "often did you skip a payment', 'when did you pay the most', 'why do you keep "
            "refusing'. Returns one summary line per day."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "First day, as YYYY-MM-DD."},
                "end": {"type": "string", "description": "Last day, as YYYY-MM-DD."},
                "reason_code": {
                    "type": "string",
                    "description": "Optional. Only return decisions carrying this reason code.",
                    "enum": [code.value for code in ReasonCode],
                },
            },
            "required": ["start", "end"],
        },
    },
]


class MalformedToolCall(ValueError):
    """The model sent a tool argument we cannot act on (e.g. '2026-13-45')."""


def _parse_day(raw: Any) -> date:
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError) as exc:
        raise MalformedToolCall(f"{raw!r} is not a YYYY-MM-DD date") from exc


def _decision_payload(record: DayRecord) -> dict[str, Any]:
    return {
        "date": record.day.isoformat(),
        "outcome": record.decision.action.value,
        "amount": str(record.decision.amount),
        "paid_off": record.paid_off,
        "reason_codes": [code.value for code in record.decision.codes],
        # The engine's own sentences. The model narrates *from* these rather than being
        # handed raw codes to paraphrase — the copy is already written, and correctly.
        "explanation": list(explain(record.decision)),
        "checking_balance": str(record.checking_balance),
        "savings_balance": str(record.savings_balance),
        "buffer_floor": str(record.buffer_floor),
        "card_balance": str(record.debt_balance),
    }


def run_tool(name: str, args: dict[str, Any], artifact: Artifact, facts: Facts) -> Any:
    """Execute one tool against the in-memory artifact, recording what it exposed.

    Every record this returns is added to `facts` — that is what later licenses the model
    to talk about it. A decision the model never fetched is a decision it cannot mention.
    """
    if name == "get_decision":
        record = artifact.by_day(_parse_day(args.get("date")))
        if record is None:
            # One uniform answer for a day before the window, after it, or inside the
            # warm-up runway that was never served. Which of the three it was is our
            # business, not the user's.
            return {"found": False, "message": "No decision on record for that day."}
        facts.record(record)
        return {"found": True, "decision": _decision_payload(record)}

    if name == "list_decisions":
        start = _parse_day(args.get("start"))
        end = _parse_day(args.get("end"))
        wanted = args.get("reason_code")

        if wanted is not None and wanted not in {code.value for code in ReasonCode}:
            raise MalformedToolCall(f"{wanted!r} is not a reason code")

        found = [
            record
            for record in artifact.days
            if start <= record.day <= end
            and (wanted is None or wanted in [c.value for c in record.decision.codes])
        ]
        for record in found:
            facts.record(record)

        return {
            "count": len(found),
            "decisions": [_decision_payload(record) for record in found],
        }

    raise MalformedToolCall(f"no such tool: {name}")


# --- the guard -----------------------------------------------------------------------

# "$1,154.67", "$400.00", "$400". Deliberately narrow: a figure the model writes some other
# way is not a figure the guard can check, and the system prompt tells it to write them
# exactly as the tool returned them.
_MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")

_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

# The model is instructed to write dates as YYYY-MM-DD so they are checkable. It will also
# naturally write "March 2" or "March 2, 2026" — both are matched so a perfectly honest
# sentence isn't rejected for its date formatting.
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}  # fmt: skip
_LONG_DATE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)

# Words that assert an outcome. Kept small and unambiguous on purpose: the guard's job is to
# catch a confident false claim, not to grade English.
_SWEEP_WORDS = re.compile(r"\b(swept|paid|moved|sent|put)\b", re.IGNORECASE)
_REFUSE_WORDS = re.compile(
    r"\b(didn't|did not|no payment|nothing|refused|held|skipped|left)\b", re.IGNORECASE
)

# Distinctive phrases from `engine/explain.py`'s copy, mapped back to the code they render.
# A phrase the model uses must belong to a reason that decision actually carries.
_REASON_PHRASES: dict[ReasonCode, tuple[str, ...]] = {
    ReasonCode.WEEKLY_CAP: ("weekly limit", "weekly cap"),
    ReasonCode.PER_SWEEP_CAP: ("single payment limit", "per-payment limit"),
    ReasonCode.CLEARS_THE_CARD: ("clears the card", "cleared the card"),
    ReasonCode.NO_SURPLUS: ("nothing spare", "no surplus"),
    ReasonCode.BELOW_MIN_SWEEP: ("isn't worth moving", "not worth moving"),
    ReasonCode.INSUFFICIENT_HISTORY: ("still learning", "not enough history"),
    ReasonCode.INCOME_TOO_VARIABLE: ("income moves around", "income is too variable"),
    ReasonCode.NO_DEBT: ("no debt left", "paid off"),
    ReasonCode.INTEREST_AVOIDED: ("interest you won't pay", "interest avoided"),
    ReasonCode.IDLE_CASH_ELSEWHERE: ("in savings",),
    ReasonCode.BLACKOUT: ("paused payments",),
}


def _to_decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:  # pragma: no cover — the regex cannot produce this
        return None


def _dates_in(text: str, window: set[date]) -> set[date]:
    """Every date the text names that we could plausibly be talking about."""
    found: set[date] = set()

    for raw in _ISO_DATE.findall(text):
        try:
            found.add(date.fromisoformat(raw))
        except ValueError:
            continue

    for month, day_str, year_str in _LONG_DATE.findall(text):
        day_num = int(day_str)
        years = [int(year_str)] if year_str else sorted({d.year for d in window})
        for year in years:
            try:
                found.add(date(year, _MONTHS[month.lower()], day_num))
            except ValueError:
                continue

    return found


def _segments(text: str) -> list[str]:
    """Sentence-ish chunks. A claim and the date it is attached to live in the same one."""
    return [part for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip()]


@dataclass(frozen=True)
class Rejection:
    reason: str


def verify(text: str, facts: Facts) -> Rejection | None:
    """Check every checkable claim in `text` against what we actually fetched this turn.

    Returns `None` when the response is safe to show, or a `Rejection` naming the first
    claim that could not be supported.

    The rules, in order of how much they matter:

    1. **A dollar figure in a sentence that names a date must belong to that date's
       decision.** This is the rule that catches a recombination — every number real, the
       attribution false.
    2. **A dollar figure in a sentence that names no date** must at least appear somewhere in
       this turn's tool results. Weaker, unavoidably: with no date in the sentence there is
       nothing to bind to. It still stops a figure invented from nothing.
    3. **An outcome asserted about a date must match that decision's `Action`** — and "paid
       off" is a REFUSE carrying NO_DEBT, never a third action.
    4. **A reason phrase used about a date must belong to that decision's codes.**

    A response with no figures, no outcomes and no reason phrases passes trivially, which is
    correct: "I'd need to know which day you mean" asserts nothing and needs no evidence.
    """
    window = set(facts.by_date)

    for segment in _segments(text):
        cited = _dates_in(segment, window) & window
        unknown_dates = _dates_in(segment, window) - window

        amounts = [d for raw in _MONEY.findall(segment) if (d := _to_decimal(raw)) is not None]

        # A sentence about a day we never fetched cannot carry a claim. This is what forces
        # a fresh tool call every turn, even to restate something said earlier in the
        # conversation — the client resends history, and history is not evidence.
        if unknown_dates and (amounts or _SWEEP_WORDS.search(segment)):
            return Rejection(f"claim about {sorted(unknown_dates)[0]}, which was never fetched")

        for amount in amounts:
            if cited:
                allowed: set[Decimal] = set()
                for day in cited:
                    allowed |= facts.amounts_for(day)
                if amount not in allowed:
                    return Rejection(
                        f"${amount} is not a figure from {sorted(cited)[0]}'s decision"
                    )
            elif amount not in facts.all_amounts():
                return Rejection(f"${amount} appears in no decision fetched this turn")

        for day in cited:
            record = facts.by_date[day]
            swept = record.decision.action is Action.SWEEP

            # "didn't pay" about a sweep, or "paid" about a refusal. The refusal wording is
            # checked only when the segment doesn't also read as a sweep, because "we paid
            # nothing" trips both patterns and means exactly what the refusal means.
            asserts_sweep = bool(_SWEEP_WORDS.search(segment))
            asserts_refusal = bool(_REFUSE_WORDS.search(segment))

            if asserts_sweep and not asserts_refusal and not swept:
                return Rejection(f"{day} was a refusal, not a payment")
            if asserts_refusal and not asserts_sweep and swept:
                return Rejection(f"{day} was a payment, not a refusal")

            codes = set(record.decision.codes)
            for code, phrases in _REASON_PHRASES.items():
                if any(phrase in segment.lower() for phrase in phrases) and code not in codes:
                    return Rejection(f"{day}'s decision does not carry {code.value}")

    return None


# --- the rate cap --------------------------------------------------------------------


@dataclass
class RateCap:
    """A per-minute and per-day ceiling on turns. In-memory, single-instance (see MAX_*)."""

    per_minute: int = MAX_TURNS_PER_MINUTE
    per_day: int = MAX_TURNS_PER_DAY
    _recent: deque[float] = field(default_factory=deque)

    def allow(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now

        while self._recent and now - self._recent[0] > 86_400:
            self._recent.popleft()

        last_minute = sum(1 for stamp in self._recent if now - stamp <= 60)
        if last_minute >= self.per_minute or len(self._recent) >= self.per_day:
            return False

        self._recent.append(now)
        return True


# --- the turn ------------------------------------------------------------------------


SYSTEM = """\
You are the explain assistant for Resfi, a service that moves a household's spare cash onto \
their credit card automatically. A deterministic engine already made every decision you will \
be asked about, along with the reason for it. You did not make them and you cannot make them.

Your job is to answer a follow-up question about decisions that already happened.

Rules that are not negotiable:
- Every dollar figure, outcome, and reason you state must come from a tool result in THIS \
turn. Never carry a figure over from earlier in the conversation — look it up again.
- If a tool says there is no decision on record for a day, say exactly that. Do not guess, \
extrapolate from nearby days, or explain what "probably" happened.
- Write dollar figures exactly as the tool returned them ($400.00, not "about four hundred").
- Write dates as YYYY-MM-DD.
- Never attribute one day's figure to another day.

Today is {today} — the most recent day on record. Resolve relative dates ("last Tuesday", \
"yesterday") against that, not against the real-world date.

Tone: plain, calm, specific. A refusal to move money is not a failure and must never read \
like one — the household's money simply stayed where it was, and there was a reason. Answer \
in two or three sentences unless the question genuinely needs more.\
"""


@dataclass(frozen=True)
class Reply:
    text: str
    outcome: Outcome


def _text_of(content: Any) -> str:
    return "\n".join(block.text for block in content if getattr(block, "type", None) == "text")


def answer(
    artifact: Artifact,
    history: list[dict[str, Any]],
    message: str,
    client: Any,
    rate_cap: RateCap | None = None,
) -> Reply:
    """One user turn: retrieve, narrate, verify, and fail closed.

    `history` is the client-held conversation, resent each turn — there is no server-side
    session store. That is what keeps "the artifact is the only state" true.
    """
    if rate_cap is not None and not rate_cap.allow():
        return Reply(RATE_LIMITED_REPLY, Outcome.RATE_LIMITED)

    facts = Facts()
    messages: list[dict[str, Any]] = [*history, {"role": "user", "content": message}]
    system = SYSTEM.format(today=artifact.window_end.isoformat())

    retried_malformed = False

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                thinking={"type": "adaptive"},
                output_config={"effort": EFFORT},
                tools=TOOLS,
                messages=messages,
                timeout=API_TIMEOUT_SECONDS,
            )
        except Exception:
            # Any transport failure — timeout, rate limit, 5xx, a dead socket. The turn is
            # unavailable. It is emphatically not an invitation to answer from memory.
            return Reply(UNAVAILABLE_REPLY, Outcome.UNAVAILABLE)

        if response.stop_reason != "tool_use":
            text = _text_of(response.content)
            break

        messages.append({"role": "assistant", "content": response.content})

        results: list[dict[str, Any]] = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            try:
                output = run_tool(block.name, dict(block.input), artifact, facts)
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": str(output)}
                )
            except MalformedToolCall as exc:
                if retried_malformed:
                    # Told once, still wrong. Fail the turn rather than let it flail.
                    return Reply(UNAVAILABLE_REPLY, Outcome.UNAVAILABLE)
                retried_malformed = True
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Invalid argument: {exc}. Dates must be YYYY-MM-DD.",
                        "is_error": True,
                    }
                )

        messages.append({"role": "user", "content": results})
    else:
        # Ran out of rounds still calling tools. Something is wrong with the loop, not with
        # the data — and a half-formed answer is worse than none.
        return Reply(UNAVAILABLE_REPLY, Outcome.UNAVAILABLE)

    if not text.strip():
        return Reply(UNAVAILABLE_REPLY, Outcome.UNAVAILABLE)

    if facts.empty:
        # It answered without looking anything up. Either it needed nothing (a clarifying
        # question, which asserts nothing and verifies trivially) or it answered from
        # memory — which is exactly the thing this module exists to prevent.
        if verify(text, facts) is not None:
            return Reply(NO_RECORD_REPLY, Outcome.NO_RECORD)
        return Reply(text, Outcome.ANSWERED)

    if (rejection := verify(text, facts)) is not None:
        # A caught hallucination. The user gets the honest answer, not the confident one.
        # The rejection reason is deliberately not shown to them — it is for our logs.
        del rejection
        return Reply(NO_RECORD_REPLY, Outcome.GUARD_REJECTED)

    return Reply(text, Outcome.ANSWERED)


def build_client() -> Any:
    """The Anthropic client, or a startup-time failure if the key is missing."""
    import anthropic

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. The assistant endpoint cannot work without it."
        )

    # One retry, not the SDK's default two: a user watching a spinner in a modal would
    # rather be told to try again than wait through three attempts of a failing API.
    return anthropic.Anthropic(timeout=API_TIMEOUT_SECONDS, max_retries=1)

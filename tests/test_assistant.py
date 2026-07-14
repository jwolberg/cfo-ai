"""The explain assistant, and mostly: the guard.

The happy path here is three tests. The rest of this file is an attempt to get a false
financial claim past the verification guard, because that — not the system prompt — is what
actually enforces "the assistant never states a claim it can't trace to a Decision."

No test in this file calls Anthropic. A fake client replays scripted responses, which lets us
put words in the model's mouth that a real model would rarely say — including the ones we
most need to be sure are caught.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from backend import artifact as art
from backend import assistant
from backend.assistant import Facts, Outcome, RateCap, answer, run_tool, verify
from engine.models import Action, ReasonCode


@pytest.fixture(scope="module")
def artifact() -> art.Artifact:
    return art.load()


@pytest.fixture(scope="module")
def sweep_day(artifact: art.Artifact) -> art.DayRecord:
    return next(r for r in artifact.days if r.decision.action is Action.SWEEP)


@pytest.fixture(scope="module")
def refuse_day(artifact: art.Artifact) -> art.DayRecord:
    return next(r for r in artifact.days if r.decision.action is Action.REFUSE)


def facts_for(*records: art.DayRecord) -> Facts:
    facts = Facts()
    for record in records:
        facts.record(record)
    return facts


# --- the fake model ------------------------------------------------------------------


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    name: str
    input: dict[str, Any]
    id: str = "toolu_1"
    type: str = "tool_use"


@dataclass
class FakeResponse:
    content: list[Any]
    stop_reason: str


class FakeClient:
    """Replays a script of responses. Records what it was asked, so tests can assert on it."""

    def __init__(self, *responses: FakeResponse | Exception) -> None:
        self.script = list(responses)
        self.calls: list[dict[str, Any]] = []

    @property
    def messages(self) -> FakeClient:
        return self

    def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("the model was called more times than the test scripted")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def says(text: str) -> FakeResponse:
    return FakeResponse([TextBlock(text)], stop_reason="end_turn")


def calls(name: str, **args: Any) -> FakeResponse:
    return FakeResponse([ToolUseBlock(name=name, input=args)], stop_reason="tool_use")


# --- tools ---------------------------------------------------------------------------


class TestTools:
    def test_a_known_day_comes_back_with_the_engines_own_words(
        self, artifact: art.Artifact, sweep_day: art.DayRecord
    ) -> None:
        facts = Facts()

        result = run_tool("get_decision", {"date": sweep_day.day.isoformat()}, artifact, facts)

        assert result["found"] is True
        assert result["decision"]["explanation"]  # rendered by engine/explain.py, not invented
        assert sweep_day.day in facts.by_date

    def test_before_after_and_warm_up_all_look_identical(self, artifact: art.Artifact) -> None:
        """Covers AE3. Three different reasons we have nothing, one answer.

        Distinguishing them would leak the shape of the demo — the user asked whether we did
        something that day, and "we have nothing on record" is the whole of the true answer.
        """
        facts = Facts()

        before = run_tool("get_decision", {"date": "2025-06-01"}, artifact, facts)
        warm_up = run_tool("get_decision", {"date": "2026-02-01"}, artifact, facts)
        after = run_tool("get_decision", {"date": "2027-01-01"}, artifact, facts)

        assert (
            before
            == warm_up
            == after
            == {
                "found": False,
                "message": "No decision on record for that day.",
            }
        )
        assert facts.empty  # nothing was fetched, so nothing may be claimed

    def test_a_range_can_be_filtered_to_one_reason(self, artifact: art.Artifact) -> None:
        facts = Facts()

        result = run_tool(
            "list_decisions",
            {"start": "2026-03-01", "end": "2026-05-30", "reason_code": "interest_avoided"},
            artifact,
            facts,
        )

        assert result["count"] > 0
        assert all("interest_avoided" in d["reason_codes"] for d in result["decisions"])

    def test_a_nonsense_date_is_malformed_not_empty(self, artifact: art.Artifact) -> None:
        """It must raise rather than quietly return "no record" — a model that sent garbage
        needs to be told, not handed an answer that looks like a fact about the household."""
        with pytest.raises(assistant.MalformedToolCall):
            run_tool("get_decision", {"date": "2026-13-45"}, artifact, Facts())


# --- the guard -----------------------------------------------------------------------


class TestGuard:
    def test_the_truth_passes(self, sweep_day: art.DayRecord) -> None:
        facts = facts_for(sweep_day)
        text = f"On {sweep_day.day}, we paid ${sweep_day.decision.amount} toward your card."

        assert verify(text, facts) is None

    def test_a_response_that_asserts_nothing_needs_no_evidence(self) -> None:
        """ "Which day did you mean?" is a legitimate answer with no claim in it."""
        assert verify("Which day did you mean?", Facts()) is None

    def test_an_invented_dollar_amount_is_caught(self, sweep_day: art.DayRecord) -> None:
        """Covers AE4. The figure is plausible, well-formatted, and from nowhere."""
        facts = facts_for(sweep_day)
        text = f"On {sweep_day.day}, we paid $1,234.56 toward your card."

        rejection = verify(text, facts)

        assert rejection is not None
        assert "1234.56" in rejection.reason.replace(",", "")

    def test_a_real_figure_attributed_to_the_wrong_day_is_caught(
        self, artifact: art.Artifact
    ) -> None:
        """Covers AE4 (adversarial). Every number here is real. The sentence is still false.

        This is the case a presence-only guard waves through: the model fetched two genuine
        decisions and quoted one's sweep against the other's date. Binding each figure to the
        `(date, field)` it was actually fetched for is the only thing that catches it.
        """
        sweeps = [r for r in artifact.days if r.decision.action is Action.SWEEP]
        a, b = next(
            (x, y)
            for x in sweeps
            for y in sweeps
            if x.decision.amount != y.decision.amount and x.day != y.day
        )
        facts = facts_for(a, b)

        honest = f"On {a.day} we paid ${a.decision.amount}."
        recombined = f"On {b.day} we paid ${a.decision.amount}."

        assert verify(honest, facts) is None
        assert verify(recombined, facts) is not None

    def test_calling_a_refusal_a_payment_is_caught(self, refuse_day: art.DayRecord) -> None:
        """Covers AE4. The outcome, not just the number, has to be true."""
        facts = facts_for(refuse_day)

        rejection = verify(f"On {refuse_day.day} we paid $0.00 toward your card.", facts)

        assert rejection is not None
        assert "refusal" in rejection.reason

    def test_calling_a_payment_a_refusal_is_caught(self, sweep_day: art.DayRecord) -> None:
        facts = facts_for(sweep_day)

        rejection = verify(f"On {sweep_day.day} we didn't move any money.", facts)

        assert rejection is not None
        assert "payment" in rejection.reason

    def test_a_fabricated_reason_is_caught_even_with_the_right_amount(
        self, sweep_day: art.DayRecord
    ) -> None:
        """Covers AE4. Right money, right outcome, invented explanation.

        The reason is the product. A decision explained by a cause that wasn't the cause is
        exactly the failure `engine/explain.py`'s code-not-prose discipline exists to stop,
        and the guard has to enforce it as hard as it enforces the dollars.
        """
        facts = facts_for(sweep_day)
        assert not sweep_day.decision.has(ReasonCode.WEEKLY_CAP)  # the seed's own data

        text = (
            f"On {sweep_day.day} we paid ${sweep_day.decision.amount} — that was all your "
            "weekly limit allowed."
        )

        rejection = verify(text, facts)

        assert rejection is not None
        assert "weekly_cap" in rejection.reason

    def test_a_claim_about_a_day_never_fetched_is_caught(self, sweep_day: art.DayRecord) -> None:
        """The rule that forces a fresh tool call every turn.

        The client resends the conversation each turn, so the model can see what it said
        three messages ago. That history is the client's word, not the engine's — restating
        a figure from it without looking the day up again is not evidence, and the guard
        treats it as a fabrication.
        """
        facts = facts_for(sweep_day)

        rejection = verify("On 2026-04-15 we paid $250.00 toward your card.", facts)

        assert rejection is not None
        assert "never fetched" in rejection.reason

    def test_a_long_form_date_is_understood(self, sweep_day: art.DayRecord) -> None:
        """The model writes "March 2, 2026" as readily as "2026-03-02". A guard that only
        parsed ISO would silently stop binding figures to dates on the friendlier phrasing —
        failing open exactly where the prose reads most natural."""
        facts = facts_for(sweep_day)
        month = sweep_day.day.strftime("%B")
        pretty = f"{month} {sweep_day.day.day}, {sweep_day.day.year}"

        assert verify(f"On {pretty} we paid ${sweep_day.decision.amount}.", facts) is None
        assert verify(f"On {pretty} we paid $9,999.00.", facts) is not None

    def test_figures_the_engine_did_emit_are_quotable(self, sweep_day: art.DayRecord) -> None:
        """The guard must not be so strict it rejects the truth. Every Decimal the engine put
        in a reason's params — the projected low, the buffer, the interest avoided — is a
        real fact about that decision and the assistant is allowed to say it."""
        facts = facts_for(sweep_day)
        interest = next(
            r.params["amount"]
            for r in sweep_day.decision.reasons
            if r.code is ReasonCode.INTEREST_AVOIDED
        )

        text = f"On {sweep_day.day}, that saved you about ${interest} of interest you won't pay."

        assert verify(text, facts) is None


# --- the turn ------------------------------------------------------------------------


class TestTurn:
    def test_a_follow_up_resolves_to_a_tool_call_and_an_answer(
        self, artifact: art.Artifact, sweep_day: art.DayRecord
    ) -> None:
        client = FakeClient(
            calls("get_decision", date=sweep_day.day.isoformat()),
            says(f"On {sweep_day.day} we paid ${sweep_day.decision.amount} toward your card."),
        )

        reply = answer(artifact, [], "why did you pay that day?", client)

        assert reply.outcome is Outcome.ANSWERED
        assert str(sweep_day.decision.amount) in reply.text

    def test_the_window_is_the_assistants_today(self, artifact: art.Artifact) -> None:
        """ "Last Tuesday" has to resolve against the demo's fixed window, not the wall clock —
        otherwise every relative date in the conversation points outside the data."""
        client = FakeClient(says("Which day did you mean?"))

        answer(artifact, [], "what about last Tuesday?", client)

        assert artifact.window_end.isoformat() in client.calls[0]["system"]

    def test_a_caught_hallucination_becomes_no_record(
        self, artifact: art.Artifact, sweep_day: art.DayRecord
    ) -> None:
        """Covers AE4 end to end: the model fetches a real decision, then says something it
        cannot support, and the user never sees the sentence."""
        client = FakeClient(
            calls("get_decision", date=sweep_day.day.isoformat()),
            says(f"On {sweep_day.day} we paid $4,242.00 toward your card."),
        )

        reply = answer(artifact, [], "how much?", client)

        assert reply.outcome is Outcome.GUARD_REJECTED
        assert reply.text == assistant.NO_RECORD_REPLY
        assert "4,242" not in reply.text

    def test_answering_from_memory_without_a_tool_call_is_caught(
        self, artifact: art.Artifact
    ) -> None:
        """No tool call, but a confident figure. There is nothing behind it by construction."""
        client = FakeClient(says("On 2026-03-02 we paid $400.00 toward your card."))

        reply = answer(artifact, [], "how much on the 2nd?", client)

        assert reply.outcome is Outcome.NO_RECORD

    def test_a_timeout_says_so_rather_than_inventing_an_answer(
        self, artifact: art.Artifact
    ) -> None:
        client = FakeClient(TimeoutError("upstream timed out"))

        reply = answer(artifact, [], "why?", client)

        assert reply.outcome is Outcome.UNAVAILABLE
        assert reply.text == assistant.UNAVAILABLE_REPLY

    def test_a_malformed_tool_call_is_retried_once_then_fails_closed(
        self, artifact: art.Artifact
    ) -> None:
        client = FakeClient(
            calls("get_decision", date="the fifteenth"),
            calls("get_decision", date="still not a date"),
        )

        reply = answer(artifact, [], "why?", client)

        assert reply.outcome is Outcome.UNAVAILABLE
        # It was told the first argument was bad, and given a chance to fix it.
        error_results = [
            block
            for call in client.calls
            for msg in call["messages"]
            if isinstance(msg.get("content"), list)
            for block in msg["content"]
            if isinstance(block, dict) and block.get("is_error")
        ]
        assert error_results

    def test_a_runaway_tool_loop_is_bounded(
        self, artifact: art.Artifact, sweep_day: art.DayRecord
    ) -> None:
        """A model paging through the window one day at a time is a cost problem, not an
        answer. It gets cut off rather than allowed to run."""
        client = FakeClient(
            *[calls("get_decision", date=sweep_day.day.isoformat()) for _ in range(10)]
        )

        reply = answer(artifact, [], "tell me everything", client)

        assert reply.outcome is Outcome.UNAVAILABLE
        assert len(client.calls) == assistant.MAX_TOOL_ROUNDS

    def test_the_rate_cap_stops_a_leaked_key_running_up_a_bill(
        self, artifact: art.Artifact
    ) -> None:
        """The API key ships inside the client bundle and is recoverable. This cap — not the
        key — is what actually bounds the Anthropic spend when that is used."""
        cap = RateCap(per_minute=2, per_day=100)
        client = FakeClient(*[says("Which day did you mean?") for _ in range(2)])

        for _ in range(2):
            assert answer(artifact, [], "hi", client, rate_cap=cap).outcome is Outcome.ANSWERED

        blocked = answer(artifact, [], "hi", client, rate_cap=cap)

        assert blocked.outcome is Outcome.RATE_LIMITED
        assert len(client.calls) == 2  # the third never reached Anthropic


class TestFacts:
    def test_a_decisions_own_decimals_are_the_only_quotable_figures(
        self, sweep_day: art.DayRecord
    ) -> None:
        facts = facts_for(sweep_day)
        amounts = facts.amounts_for(sweep_day.day)

        assert sweep_day.decision.amount in amounts
        assert sweep_day.checking_balance in amounts
        assert Decimal("999999.99") not in amounts

    def test_an_unfetched_day_supports_nothing(self, sweep_day: art.DayRecord) -> None:
        facts = facts_for(sweep_day)

        assert facts.amounts_for(date(2026, 4, 15)) == set()

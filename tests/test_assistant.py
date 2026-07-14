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
from engine.models import Action, Decision, Reason, ReasonCode


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


def _a_sweep_with_a_horizon(artifact: art.Artifact):
    """Any sweep day whose projection quotes a horizon date, with its real figures.

    Found rather than hardcoded, deliberately. These tests used to pin a specific day and a
    specific pair of dollar figures out of the committed artifact — so they broke the moment
    the engine's decisions legitimately changed, and the breakage read as "the guard is wrong"
    when the guard was fine. The property under test is the *word-order rule*, not which
    Tuesday the engine happened to sweep on.
    """
    from engine.models import Action

    for record in artifact.days:
        if record.decision.action is not Action.SWEEP:
            continue
        facts = facts_for(record)
        for horizon, figures in facts.supporting.items():
            if figures:
                low = f"${max(figures):,.2f}"
                return record, facts, horizon, low, f"${record.decision.amount:,.2f}"

    raise AssertionError("no sweep day quotes a projection horizon — the fixture has drifted")


def explain_sentences(record: art.DayRecord) -> list[str]:
    """The engine's own copy for a decision — the same sentences the tool payload carries."""
    from engine.explain import explain

    return list(explain(record.decision))


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

    def test_the_summary_is_fetched_not_computed(self, artifact: art.Artifact) -> None:
        """Covers the aggregate question, which previously had no tool at all.

        "How much interest have you saved me" left the model one way to answer: fetch the
        decisions and add them up. It did, and it got $4,354.79 — exactly right, and rejected,
        because a figure the model computed matches no decision the engine made. The user was
        told "I don't have that on record" about a total that is on the dashboard.

        The fix is a tool, not a looser guard. The engine already computed these.
        """
        facts = Facts()

        result = run_tool("get_summary", {}, artifact, facts)

        assert result["interest_avoided_total"] == str(artifact.summary.interest_avoided_total)
        assert result["sweep_count"] == artifact.summary.sweep_count
        # Fetched, therefore quotable — the same contract a decision gets.
        assert artifact.summary.interest_avoided_total in facts.all_amounts()
        assert not facts.empty

    def test_a_total_the_model_worked_out_itself_is_still_refused(
        self, artifact: art.Artifact
    ) -> None:
        """The tool must not become a licence to do arithmetic.

        With the summary fetched, its own figures are quotable. A *different* total — one the
        model derived rather than retrieved — still matches nothing and must still be caught.
        This is the line the new tool must not blur.
        """
        facts = Facts()
        run_tool("get_summary", {}, artifact, facts)

        honest = f"You've avoided ${artifact.summary.interest_avoided_total} in interest."
        assert verify(honest, facts) is None

        rejection = verify("You've avoided $9,999.99 in interest.", facts)
        assert rejection is not None
        assert "no decision fetched this turn" in rejection.reason

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

    def test_the_engines_own_copy_passes_on_every_day(self, artifact: art.Artifact) -> None:
        """The guard must never reject the engine.

        The most faithful answer the assistant can give is the engine's own sentences, dated.
        If the guard rejects those, the model has no honest reply available at all — the user
        is told "I don't have that on record" about a decision sitting right there in the
        artifact, and the outcome is indistinguishable from a genuine gap in the data.

        That is not hypothetical, and it is the reason this test asserts across every day
        rather than a sampled one. `CADENCE_HOLD` (added with the weekly cadence, and now the
        most common reason in the window) explains a refusal by naming the *previous* payment:
        "We paid your card 1 day ago." The guard read the verb "paid", saw a REFUSE, and
        called it a fabricated payment on 72 of these 90 days. A single-day sample would have
        landed on one of the 18 that pass and reported green.

        Any new reason code whose copy trips the guard fails here, on the day it is added.
        """
        rejected = []
        for record in artifact.days:
            facts = facts_for(record)
            text = f"On {record.day.isoformat()}, " + " ".join(explain_sentences(record))
            if (rejection := verify(text, facts)) is not None:
                rejected.append(f"{record.day}: {rejection.reason}")

        assert not rejected, (
            f"the guard rejected the engine's own copy on {len(rejected)}/{len(artifact.days)} "
            f"days: {rejected[:3]}"
        )

    def test_a_prior_payment_is_not_a_claim_about_today(self, artifact: art.Artifact) -> None:
        """ "We paid your card 3 days ago" is the reason for a refusal, not a payment claim.

        The exemption is narrow on purpose, so this pins both sides of it: the past-tense
        reference passes, and the same verb without the "ago" is still caught as a false
        payment claim on a refusal day.
        """
        cadence = next(r for r in artifact.days if ReasonCode.CADENCE_HOLD in r.decision.codes)
        facts = facts_for(cadence)
        day = cadence.day.isoformat()

        assert cadence.decision.action is Action.REFUSE
        assert verify(f"On {day}, we paid your card 1 day ago, so we held off.", facts) is None
        assert verify(f"On {day}, we paid your card.", facts) is not None

    def test_a_dollar_amount_does_not_break_the_prior_payment_exemption(
        self, artifact: art.Artifact
    ) -> None:
        """**A dollar amount contains a period, and the sentence guard used to stop at it.**

        `_PRIOR_PAYMENT` bounded the gap between the verb and the "ago" with `[^.!?]`, to keep
        the two inside one sentence. Then CADENCE_HOLD's copy started naming the figure — "We
        paid an extra $1,600.00 for you 1 day ago" — and the decimal point in `$1,600.00` hard-
        stopped the character class *inside the number*. The match died before reaching "ago",
        the guard read a past payment as a claim about today, and it rejected the engine's own
        copy on **59 of 90 days**.

        Same outage as the one `_PRIOR_PAYMENT` was written to fix, arriving through a
        character class instead of a phrase list. A sentence-ending period is followed by
        whitespace; a decimal point is followed by a digit. Only the first one ends the clause.
        """
        cadence = next(r for r in artifact.days if ReasonCode.CADENCE_HOLD in r.decision.codes)
        facts = facts_for(cadence)
        day = cadence.day.isoformat()

        # The figure the engine's own copy names: `last_sweep_amount`, carried on the reason.
        hold = next(r for r in cadence.decision.reasons if r.code is ReasonCode.CADENCE_HOLD)
        paid = f"${hold.params['amount']:,.2f}"
        assert paid in {f"${a:,.2f}" for a in facts.amounts_for(cadence.day)}, (
            "the amount the copy quotes must be a fact the guard already knows — otherwise the "
            "assistant cannot repeat the engine's own sentence"
        )

        ok = f"On {day}, we paid an extra {paid} for you 1 day ago."
        assert verify(ok, facts) is None

        # The exemption must not have been widened into a licence: the same sentence without
        # the "ago" is still a payment claim, and this day is a refusal.
        assert verify(f"On {day}, we paid an extra {paid} for you.", facts) is not None

        # And it must still not reach across a sentence boundary to borrow a neighbour's "ago".
        assert verify(f"On {day}, we paid your card. That was 1 day ago.", facts) is not None

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


class TestSupportingDates:
    """The projection horizon is data *inside* a decision, not a decision of its own.

    `engine/explain.py` writes the horizon into its sentences ("heading for a low of $748.79
    on 2026-06-05"). A model narrating the engine faithfully repeats that date — and the guard
    used to reject it as fabricated, on 82 of the 90 served days. Every test here exists to
    stop that from coming back.
    """

    def test_the_engines_own_copy_is_never_rejected(self, artifact: art.Artifact) -> None:
        """The strongest form of the rule: the engine's own words must always survive.

        Not a sample — every served day. If the guard ever rejects the very sentences the
        backend handed the model, the assistant is broken for that day by construction.
        """
        for record in artifact.days:
            facts = facts_for(record)
            text = " ".join(explain_sentences(record))
            assert verify(text, facts) is None, (
                f"guard rejected the engine's own copy for {record.day}"
            )

    def test_the_real_models_answer_survives(self) -> None:
        """Verbatim output from a live claude-opus-4-8 call, which the guard used to reject.

        Kept word for word rather than paraphrased: it is the actual failure, and a tidied-up
        version of it would not have caught the bug.

        The `DayRecord` is built here rather than pulled from the demo artifact, and that is a
        deliberate change. This test used to read 2026-05-20 out of `backend/data/decisions.json`
        — so the moment the demo's policy changed (the sweep cadence went weekly), that day's
        projection changed with it and this test failed, even though the guard it exists to
        protect had not moved a line. A regression test for the guard must not be hostage to the
        demo household's seed. These are the figures the model was actually answering about.
        """
        record = art.DayRecord(
            day=date(2026, 5, 20),
            decision=Decision(
                action=Action.REFUSE,
                amount=Decimal("0.00"),
                target_debt_id=None,
                reasons=(
                    Reason(
                        ReasonCode.NO_SURPLUS,
                        {
                            "low": Decimal("748.79"),
                            "low_day": date(2026, 6, 5),
                            "buffer": Decimal("800.00"),
                            "reserved": Decimal("280.00"),
                        },
                    ),
                ),
                projected_low_balance=Decimal("748.79"),
            ),
            checking_balance=Decimal("1892.44"),
            savings_balance=Decimal("2400.00"),
            buffer_floor=Decimal("800.00"),
            debt_balance=Decimal("4751.31"),
            debt_apr=Decimal("0.2399"),
            debt_id="card_demo",
            history_days=140,
        )

        text = (
            "On 2026-05-20 nothing moved because there was no spare cash to move: your "
            "checking balance was projected to dip to $748.79 on 2026-06-05, and once your "
            "$800.00 buffer and $280.00 in minimum payments were set aside, there was nothing "
            "left over. Your money simply stayed put."
        )
        assert verify(text, facts_for(record)) is None

    def test_a_supporting_date_may_be_mentioned_but_not_claimed_about(
        self, artifact: art.Artifact
    ) -> None:
        """The hole the fix must not open.

        Every ingredient here is real — 2026-06-05 was genuinely shown to the model, and the
        amount is genuinely from the decision. Only the attribution is invented, which is
        exactly the recombination the guard exists to catch.
        """
        record = artifact.by_day(date(2026, 5, 20))
        assert record is not None
        facts = facts_for(record)
        assert date(2026, 6, 5) in facts.supporting, (
            "fixture drift: 2026-06-05 is no longer a supporting date"
        )

        rejection = verify("On 2026-06-05 we swept $748.79 onto your card.", facts)
        assert rejection is not None
        assert "2026-06-05" in rejection.reason

    def test_a_projection_horizon_may_share_a_sentence_with_the_sweep_it_explains(
        self, artifact: art.Artifact
    ) -> None:
        """The engine explains a sweep *by* its projection, so both land in one sentence.

        "Heading for a low of $4,024.53 on 2026-04-07, so we paid $1,600.00" names a supporting
        date and a payment verb together, and every word is true. Rejecting on that
        co-occurrence alone killed real sweep-day answers at random — the identical question
        answered six times and was rejected the seventh, decided by nothing but whether the
        model happened to break the sentence in two.

        What licenses it is word order: the projection reaches the date through its own figure,
        so the figure comes first. See the attribution cases below, which must still be caught.
        """
        record, facts, horizon, low, amount = _a_sweep_with_a_horizon(artifact)

        true_and_now_allowed = [
            f"Your balance was heading for a low of {low} on {horizon}, "
            f"so the engine paid {amount} onto your card.",
            f"The projection showed a low of {low} on {horizon}, and we still moved {amount}.",
        ]
        for text in true_and_now_allowed:
            assert verify(text, facts) is None, f"the guard rejected a true sentence: {text}"

    def test_leading_with_the_horizon_date_still_attributes_and_is_caught(
        self, artifact: art.Artifact
    ) -> None:
        """The other half of the word-order rule, and the reason it is word order.

        The second sentence is the one a co-occurrence test would wave through: it quotes the
        horizon's *own* low figure, so "did the model mention the projection?" answers yes —
        and it still asserts a payment on a day that never had one. Leading with the date is
        attribution no matter what else the sentence carries.
        """
        record, facts, horizon, low, amount = _a_sweep_with_a_horizon(artifact)

        attributions = [
            f"On {horizon} we paid {amount} onto your card.",
            f"On {horizon}, with a low of {low}, we swept {amount}.",
        ]
        for text in attributions:
            rejection = verify(text, facts)
            assert rejection is not None, f"the guard passed a false attribution: {text}"
            assert str(horizon) in rejection.reason

    def test_an_unfetched_date_is_still_rejected(self, refuse_day: art.DayRecord) -> None:
        """The original rule is intact: a date we never fetched in any form carries nothing."""
        facts = facts_for(refuse_day)
        rejection = verify("On 2019-01-03 we swept $400.00 onto your card.", facts)
        assert rejection is not None
        assert "never fetched" in rejection.reason


class TestNegatedSweepVerbs:
    """ "No money moved" describes a refusal. The guard used to call it a payment claim.

    `_SWEEP_WORDS` matches the verb; `_REFUSE_WORDS` knows "no payment" but not "no money". So
    the plainest description of a refusal read as an assertion that money moved, and truthful
    answers were rejected — intermittently, depending on which phrasing the model reached for.
    Every string below is verbatim output from a live claude-opus-4-8 call.

    The shipped model is now `claude-sonnet-5` (`backend/assistant.py`), and these Opus-era
    strings are kept deliberately rather than re-captured: a guard that survives the phrasings of
    *two* models is better evidence than one tuned to whichever model happens to be configured
    today. Treat them as cross-model regression cases, not as a record of the current model.
    """

    def test_the_models_real_refusal_phrasings_pass(self, refuse_day: art.DayRecord) -> None:
        facts = facts_for(refuse_day)
        day = refuse_day.day
        for text in (
            f"On {day}, no money moved — the amount was $0.00.",
            f"On {day}, no money was moved — your cash stayed where it was.",
            f"On {day}, no money was moved — the amount was $0.00.",
        ):
            assert verify(text, facts) is None, f"guard rejected a truthful refusal: {text!r}"

    def test_a_negated_sweep_about_a_payment_day_is_still_caught(
        self, sweep_day: art.DayRecord
    ) -> None:
        """The other direction, and the hole this must not open.

        The same sentence about a day money *did* move is a false denial. Treating negation as
        a refusal assertion is what lets the guard catch it rather than shrug at it.
        """
        facts = facts_for(sweep_day)
        rejection = verify(f"On {sweep_day.day}, no money moved.", facts)
        assert rejection is not None
        assert "payment, not a refusal" in rejection.reason

    def test_an_unnegated_payment_claim_about_a_refusal_is_still_caught(
        self, refuse_day: art.DayRecord
    ) -> None:
        """AE4 stays caught: "paid $0.00 toward your card" asserts a payment that never was."""
        facts = facts_for(refuse_day)
        rejection = verify(f"On {refuse_day.day} we paid $0.00 toward your card.", facts)
        assert rejection is not None
        assert "refusal" in rejection.reason


class TestMoneyParsing:
    """A dollar figure must be read whole, or the guard rejects the truth as a fabrication.

    `$12092.26` used to parse as `$120`: the pattern's first branch took three digits, found no
    comma and no decimal point, and succeeded — and a regex alternation does not backtrack once
    a branch matches. Any figure over $1,000 written without a thousands separator was silently
    truncated, matched no real amount, and got a correct answer thrown away. The card balances
    in this artifact are $4,557.41 and $12,092.26, so this fired constantly.
    """

    def test_unseparated_thousands_are_read_whole(self) -> None:
        for text, expected in (
            ("$5321.39", "5321.39"),
            ("$12092.26", "12092.26"),
            ("$5,321.39", "5,321.39"),
            ("$400.00", "400.00"),
            ("$0.00", "0.00"),
        ):
            assert assistant._MONEY.findall(text) == [expected], f"misparsed {text!r}"

    def test_a_correctly_quoted_balance_passes(self, refuse_day: art.DayRecord) -> None:
        """The figure the model quotes is real; only the regex made it look invented."""
        facts = facts_for(refuse_day)
        balance = refuse_day.debt_balance  # e.g. Decimal("12092.26")
        text = f"On {refuse_day.day} nothing moved. Your card balance stood at ${balance}."

        assert verify(text, facts) is None

    def test_an_invented_four_figure_amount_is_still_caught(
        self, refuse_day: art.DayRecord
    ) -> None:
        """Reading the number whole must not mean waving it through."""
        facts = facts_for(refuse_day)
        rejection = verify(f"On {refuse_day.day} your card balance stood at $9999.99.", facts)
        assert rejection is not None

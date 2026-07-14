---
id: 0003
title: Structured tool-calling over embeddings, and a guard over a system prompt
anchor: ADR-0003
status: accepted
date: 2026-07-13
supersedes:
superseded-by:
---

## [1] Context

The explain assistant (`docs/plans/2026-07-13-001-feat-decision-engine-frontend-mvp-plan.md`
§U4) answers follow-up questions about the household's own decision history: *"why not last
Tuesday?"*, *"how often did you skip a payment?"*. Two questions had to be settled.

**How does it retrieve?** The origin doc left this open, with RAG-over-embeddings as the
assumed default.

**How is the no-fabrication guarantee enforced?** `docs/architecture.md` §3.5 is unambiguous:
*"the LLM narrates the decision's reasons; it never produces them."* The requirement (R6) is
that the assistant never states a financial claim it can't trace to a `Decision`. The
question is what *enforces* that.

The data being retrieved is 90 records of small, uniform, structured data with a natural
primary key (the date) — not prose, not documents, not a corpus.

## [2] Decision

**Retrieval is two structured tools over the in-memory artifact**, not embeddings:

- `get_decision(date)` — one day.
- `list_decisions(start, end, reason_code=None)` — a range, optionally filtered.

No vector store, no chunking, no embedding model, no similarity threshold to tune.

**The no-fabrication guarantee is enforced by a response-verification guard in code**
(`backend/assistant.py`'s `verify()`), not by the system prompt. Before any reply reaches the
user, every checkable claim in it is matched against the tool results *from that turn*:

1. **Dollar figures are exact-matched** against the `Decimal`s the tool actually returned —
   not tolerance-matched. The source values are already precise (`money()`), and a tolerance
   would let a near-miss hallucination through.
2. **Figures are bound to the `(date, field)` they are attributed to**, not pooled across the
   turn. A sentence naming a date may only carry that decision's figures.
3. **The asserted outcome must match the decision's `Action`.** "Paid off" is a `REFUSE`
   carrying `NO_DEBT` — never a third action.
4. **A reason phrase used about a date must belong to that decision's codes.**

Any claim that doesn't verify replaces the **whole** response with "no record."

The loop is bounded (5 tool round-trips per turn), each Anthropic call has an explicit
timeout, a malformed tool argument gets one corrective retry and then fails closed, and the
endpoint carries an in-process rate cap.

## [3] Consequences

### On retrieval

Retrieval is exact rather than approximate. "What happened on 2026-03-06" is a dictionary
lookup, not a nearest-neighbour search that might return 2026-03-05 because the text was
similar. For structured records keyed by date, embeddings would introduce a failure mode
(retrieving the *wrong but similar* decision) that a key lookup does not have — and would do
it while adding an embedding model, a vector store, and a similarity threshold to tune.

This decision **is scoped to structured data**. If the assistant ever needs to answer over
prose — policy documents, support articles, a knowledge base — embeddings become the right
tool and this ADR does not argue otherwise.

### On the guard

**The system prompt is a request; the guard is the contract.** A prompt asking a model not to
invent numbers is a strong request that usually works. "Usually" is not a guarantee, and this
product's entire claim is that it will not tell a household something false about their own
money. So the guarantee is enforced where it can be tested — and it *is* tested: most of
`tests/test_assistant.py` is an attempt to get a false claim past it.

The rule that earns its keep is **(2), the per-`(date, field)` binding**. The failure it
catches is the one a naive guard misses: the model fetches two real decisions and quotes one
day's sweep against the other day's date. Every number in the sentence is real. A
presence-only check ("is $400 anywhere in the tool results?") passes it, and the user is told
something false assembled entirely from true parts.

### What the guard cannot do, stated plainly

- **A figure in a sentence naming no date** can only be checked against the union of the
  turn's fetched decisions, not bound to one. Weaker — unavoidably, since there is nothing to
  bind it to. It still catches a number invented from nothing.
- **Extraction is regex over the final text**, not semantic understanding. A claim phrased so
  as to contain no parseable figure, outcome word, or known reason phrase is not checked. The
  guard raises the cost of a *specific, quotable* fabrication — the kind that would actually
  mislead someone about their money — rather than proving the prose is true in general.
- It **cannot** stop the model from being vague, unhelpful, or wrong about something that
  isn't a financial claim.

Accepting those limits is deliberate: a guard that tried to verify every sentence
semantically would need a second model, and would then have the same problem one level up.

### Three outcomes, deliberately kept apart

`Outcome` distinguishes a tool's honest **no record** (we were asked about a day we have
nothing for), a **guard rejection** (the model tried to fabricate and was caught), and
**unavailable** (Anthropic timed out or the loop ran away). Two of them render similar copy.
They are entirely different events, and conflating them in a log would hide the only metric
that matters here — *how often does the model try?*

## [4] Conflicts resolved

- **C1 — embeddings/vector store vs structured tool-calling:** chose tool-calling. The data
  is structured and keyed; approximate retrieval buys nothing and can return the wrong record.
- **C2 — system-prompt instruction vs code-enforced guard:** chose the guard. An instruction
  is not an enforcement mechanism, and R6 is a guarantee, not a preference.
- **C3 — exact vs tolerance matching on dollar figures:** chose exact. The values are already
  precise `Decimal`s; a tolerance exists only to let a wrong number through.
- **C4 — presence check vs per-`(date, field)` binding:** chose binding. Presence-only passes
  a recombination of real values into a false claim.

## [5] Unchanged and still binding

- The LLM is **not in the decision path** and never will be. `decide()` ran at build time.
- **Base narration never calls the model.** Tapping a decision renders `engine/explain.py`
  directly (`GET /decisions/{date}/explain`). The most-viewed text in the product is the one
  thing that cannot hallucinate, and an Anthropic outage cannot take the dashboard down.
- Reasons remain **codes, not sentences** (`engine/explain.py`). The assistant narrates from
  the engine's own rendered copy rather than paraphrasing a financial claim.

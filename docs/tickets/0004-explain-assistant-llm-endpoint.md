---
id: "0004"
title: Explain-assistant LLM endpoint
type: security
status: done
priority: high
repo: cfo-ai
agentId: backend-llm-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-13-001-feat-decision-engine-frontend-mvp-plan.md
depends_on: ["0001", "0002"]
created: 2026-07-13
---

# Explain-assistant LLM endpoint

Implements **U4** of the plan. Single owner: `backend-llm-agent` (distinct specialty from
the general backend work — this is the plan's most novel, safety-critical unit: the
Claude tool-use integration and the anti-hallucination response-verification guard).

**Depends on:** `0001` (artifact), `0002` (service scaffold).

**Files:** `backend/assistant.py`,
`docs/decisions/0003-structured-tool-calling-over-embeddings.md`,
`tests/test_assistant.py`

**Key points (safety-critical — see plan's Key Technical Decisions for full contract):**
- `POST /assistant/message` — tools: `get_decision(date)`, `list_decisions(start, end,
  reason_code=None)` over the in-memory artifact. No embeddings/vector store.
- Tool-call loop capped at 5 round-trips/turn, explicit per-call timeout, in-process rate
  cap (only authoritative once `0009`/U8b pins `--max-instances=1`).
- Response-verification guard: exact-matches (not tolerance-matched) dollar amounts,
  `Action` (`SWEEP`/`REFUSE` — `NO_DEBT` is a reason code on `REFUSE`, not a third
  action), and reason code against the specific `(date, field)` triples the backend
  itself recorded from this turn's actual tool calls — extraction is regex/number-parsing
  over the model's final text, not free-form NLP interpretation. Any unverifiable claim
  replaces the whole response with "no record."
- Recommended: a small throwaway spike of the guard's matching logic against synthetic
  tool-result data before building out the rest of this unit, since it has no existing
  precedent in the codebase to adapt from.

**Verification:** `tests/test_assistant.py` passes, including the adversarial
correct-amount-wrong-decision and correct-amount-wrong-reason-code test cases.

Full detail: `docs/plans/2026-07-13-001-feat-decision-engine-frontend-mvp-plan.md` § U4.

---
id: "0003"
title: Base narration endpoint
type: feature
status: open
priority: medium
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-13-001-feat-decision-engine-frontend-mvp-plan.md
depends_on: ["0001", "0002"]
created: 2026-07-13
---

# Base narration endpoint

Implements **U3** of the plan. Single owner: `backend-python-agent`.

**Goal:** Serve summary stats and plain-language decision narration with zero LLM/network
dependency.

**Depends on:** `0001` (artifact), `0002` (service scaffold).

**Files:** modify `backend/main.py`, `tests/test_backend_api.py`

**Key points:**
- `GET /decisions/{date}/explain` calls `engine.explain.explain()`/`render()` directly —
  no LLM call in this path at all.
- A date not in the served window returns a clear "no record" response, not a 500 (same
  posture as U4's tool responses).
- Summary stats (interest avoided, buffer, targeted debt) computed once from the served
  window.

**Verification:** Every date in the served window resolves to non-empty, tone-correct
narration with zero outbound network calls.

Full detail: `docs/plans/2026-07-13-001-feat-decision-engine-frontend-mvp-plan.md` § U3.

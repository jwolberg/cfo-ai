---
id: "0007"
title: Explain assistant modal
type: feature
status: open
priority: medium
repo: cfo-ai
agentId: mobile-rn-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-13-001-feat-decision-engine-frontend-mvp-plan.md
depends_on: ["0003", "0004", "0006"]
created: 2026-07-13
---

# Explain assistant modal

Implements **U7** of the plan. Single owner: `mobile-rn-agent`.

**Goal:** Implement tap-to-explain and the follow-up conversation UI.

**Depends on:** `0003` (base narration), `0004` (LLM assistant endpoint), `0006`
(dashboard, which this modal opens from).

**Files:** `mobile/src/screens/ExplainModal.tsx`,
`mobile/src/screens/ExplainModal.test.tsx`

**Key points:**
- Full-screen modal (not a partial overlay — a full-screen modal blocks the dashboard
  underneath, so there's no "switch decisions while open" case to build; every open is a
  fresh mount). Explicit close (X) control + Android hardware-back both return to the
  dashboard.
- Base narration fetch on open has its own loading/timeout/fallback state (same pattern
  as the dashboard's).
- Free-text follow-up input; disabled with a "thinking" indicator while a turn is in
  flight (the backend's multi-round-trip tool-call loop can take several real seconds).
- Per-turn failure renders an inline error bubble without closing the modal or
  fabricating a reply.

**Verification:** Manual walkthrough of tap → base narration → follow-up (visible
thinking state) → response → close, confirming no LLM call before the first follow-up.

Full detail: `docs/plans/2026-07-13-001-feat-decision-engine-frontend-mvp-plan.md` § U7.

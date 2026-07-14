---
id: "0008"
title: "CI: lint/test backend + typecheck mobile"
type: dx
status: open
priority: medium
repo: cfo-ai
agentId: infra-devops-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-13-001-feat-decision-engine-frontend-mvp-plan.md
depends_on: ["0002", "0005"]
created: 2026-07-13
---

# CI: lint/test backend + typecheck mobile

Implements the CI-extension half of the plan's **U8**, split out from deployment because
it depends on a different pair of prerequisites (backend + mobile existing) than the
deploy step does (backend only). Single owner: `infra-devops-agent`.

**Depends on:** `0002` (backend package to lint/test), `0005` (mobile package to
typecheck).

**Files:** modify `.github/workflows/ci.yml`

**Key points:**
- Extend the existing `quality` job (or add a parallel one) to lint/test `backend`
  alongside `engine`.
- Add a lightweight `mobile` job running `tsc --noEmit` — full component test suite in CI
  is out of scope for this MVP (see plan's Scope Boundaries → Deferred to Follow-Up
  Work).

**Verification:** CI is green on a PR touching `backend/` and `mobile/`.

**Recommended (not strictly blocking):** land before `0009` so deployment ships behind a
green CI gate, matching the plan's own ordering.

Full detail: `docs/plans/2026-07-13-001-feat-decision-engine-frontend-mvp-plan.md` § U8.

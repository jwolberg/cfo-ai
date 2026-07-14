---
id: "0002"
title: FastAPI service scaffold
type: feature
status: open
priority: medium
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-13-001-feat-decision-engine-frontend-mvp-plan.md
depends_on: ["0001"]
created: 2026-07-13
---

# FastAPI service scaffold

Implements **U2** of the plan. Single owner: `backend-python-agent`.

**Goal:** Stand up the backend service: load and validate the artifact at startup,
expose a decision-history endpoint, and gate the service behind an API key.

**Depends on:** `0001` (needs the generated artifact to load and validate).

**Files:** `backend/main.py`, `backend/auth.py`, `backend/requirements.txt`,
`pyproject.toml` (add `backend` to `packages`), `tests/test_backend_api.py`

**Key points:**
- Startup fails fast (refuses to start) on a malformed/missing artifact — never
  500-per-request.
- `GET /decisions` returns the served window; `fastapi.security.APIKeyHeader` gates all
  routes except `/healthz`.
- `CORSMiddleware` with an explicit allowed-origins list (required for the Expo-web
  verification path in U5/U6 — see plan's Key Technical Decisions).

**Verification:** Service starts against the real artifact, `/healthz` and `/decisions`
respond as expected, `tests/test_backend_api.py` passes.

Full detail: `docs/plans/2026-07-13-001-feat-decision-engine-frontend-mvp-plan.md` § U2.

---
id: "0009"
closed: 2026-07-16
title: Cloud Run deployment
type: feature
status: done
priority: medium
repo: cfo-ai
agentId: infra-devops-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-13-001-feat-decision-engine-frontend-mvp-plan.md
depends_on: ["0002", "0003", "0004", "0008"]
created: 2026-07-13
---

# Cloud Run deployment

Implements the deployment half of the plan's **U8**, split out from CI because it depends
on the backend being fully built (all three endpoints) rather than just scaffolded.
Single owner: `infra-devops-agent`.

**Depends on:** `0002`, `0003`, `0004` (backend must be complete to deploy). Recommended
after `0008` (CI green) though not strictly blocking.

**Files:** `backend/Dockerfile` (only if buildpacks can't infer the build)

**Key points:**
- `gcloud run deploy --source .`, `--min-instances=1 --max-instances=1` — the max-pin is
  required, not optional: the in-process rate cap on `/assistant/message` (see `0004`)
  is only a real bound on Anthropic spend if exactly one instance ever runs.
- Container listens on `0.0.0.0:$PORT`, single Uvicorn worker, `PYTHONUNBUFFERED=1`.
- API key and Anthropic API key set via `gcloud run deploy --set-secrets` (Secret
  Manager-backed), never plain env vars, never committed.
- `CORSMiddleware`'s allowed-origins list configured for the deployed Expo-web origin.
- The precompute-generated JSON artifact (`0001`) is committed to the repo — buildpacks
  package it automatically, no separate build step needed.

**Verification:** A deployed Cloud Run URL responds to `/healthz` and (with the API key)
`/decisions`.

Full detail: `docs/plans/2026-07-13-001-feat-decision-engine-frontend-mvp-plan.md` § U8.

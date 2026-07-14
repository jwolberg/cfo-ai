---
id: "0005"
title: Expo app scaffold
type: feature
status: done
priority: low
repo: cfo-ai
agentId: mobile-rn-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-13-001-feat-decision-engine-frontend-mvp-plan.md
depends_on: ["0002"]
created: 2026-07-13
---

# Expo app scaffold

Implements **U5** of the plan. Single owner: `mobile-rn-agent`.

**Goal:** Stand up the RN/TypeScript client and wire it to the backend.

**Depends on:** `0002` (needs a backend to point the API client at — verification
requires a live `/decisions` endpoint, though the scaffold itself can start before U2 is
fully done in practice).

**Files:** `mobile/` (Expo `blank-typescript` scaffold), `mobile/src/api/client.ts`,
`mobile/src/api/client.test.ts`

**Key points:**
- `npx create-expo-app@latest mobile --template blank-typescript`.
- `client.ts` centralizes the base URL and API key header — no other file constructs
  requests directly.

**Verification:** `npx expo start --web` runs the scaffold against the deployed (or
locally-run) backend and successfully fetches `/decisions`.

Full detail: `docs/plans/2026-07-13-001-feat-decision-engine-frontend-mvp-plan.md` § U5.

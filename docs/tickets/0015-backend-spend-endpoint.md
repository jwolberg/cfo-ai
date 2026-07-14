---
id: "0015"
title: Backend — GET /spend
type: feature
status: done
priority: medium
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-14-001-feat-card-spend-portfolio-reserve-plan.md
depends_on: ["0012"]
created: 2026-07-14
---

# Backend — GET /spend

Implements **U6** of the plan. Single owner: `backend-python-agent`.

**Goal:** Serve the spend profile. No route exists today.

**Depends on:** `0012` (derivation).

**Files:** `backend/main.py`, `backend/artifact.py`, `tests/test_main.py`, `tests/test_artifact.py`

**Key points:**
- A new authenticated route returning the spend profile, this cycle's statement/unbilled split, and
  the rolling 30-day series.
- **Money crosses the wire as strings, never JSON numbers**, per the existing `usd()` convention. A
  float here is a rounding bug with a long fuse.
- `SCHEMA_VERSION` bumps; the committed artifact must validate against it.
- This endpoint **feeds no decision**. It is comprehension only.

**Test scenarios:** `GET /spend` without an API key is rejected; every money field serializes as a
string and round-trips through `from_json` without precision loss; the committed artifact validates
against the bumped schema version; a household with no card history returns an empty profile rather
than erroring.

**Verification:** Route serves; artifact round-trips.

Full detail: `docs/plans/2026-07-14-001-feat-card-spend-portfolio-reserve-plan.md` § U6.

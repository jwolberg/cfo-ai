---
id: "0016"
title: Mobile — bottom tabs, Spending screen, attestation gate
type: feature
status: todo
priority: medium
repo: cfo-ai
agentId: mobile-rn-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-14-001-feat-card-spend-portfolio-reserve-plan.md
depends_on: ["0015"]
created: 2026-07-14
---

# Mobile — bottom tabs, Spending screen, attestation gate

Implements **U7** of the plan. Single owner: `mobile-rn-agent`.

**Goal:** The spend-comprehension surface, and the onboarding attestation that the coverage decision
made load-bearing.

**Depends on:** `0015` (`GET /spend`).

**Files:** `mobile/App.tsx`, `mobile/src/api/types.ts`, `mobile/src/api/client.ts`,
`mobile/src/screens/Spending.tsx`, `mobile/src/screens/Spending.test.tsx`, `mobile/src/components/`

**Navigation.** Bottom tabs — `Decisions` | `Spending`. This retires `App.tsx`'s "no router" stance
**deliberately**: comprehension is now a co-equal surface, not a detail of a sweep.

**Three panels:**

1. **This cycle.** The headline — what has been charged, when it closes, when it is due. Then the two
   obligations *separated*, because they are due a month apart: the closed statement (inside the
   horizon, reserved) and the unbilled balance (outside it, not yet reserved). And the line that ties
   the dashboard to the engine: **"We're holding back $2,240 of your cash for this."** The reserve
   stops being arbitrary.
2. **What normal looks like.** Three strata, because a single "monthly spend" number hides all of it:
   the **floor** they cannot flex (recurring commitments, summed); the **band** (per-category median /
   p90 / worst month); and the **tail** (one-offs — *"four times last year you spent over $800 in a
   week"*). The tail is what the buffer is actually for, and it is invisible in a monthly average.
3. **Your worst month.** A strip chart of every overlapping 30-day total from the trailing year, p90
   and worst marked. *"Your worst 30-day stretch last year: $2,231. We reserve against months like
   that one."*

**When the sweep is not the answer.** If charges outrun payments, say so plainly:

> **Your card grew $310 last month.** You charged $1,760 and paid $1,450. A sweep will not catch that
> up — the spending is the thing to change.

The engine already refuses to claim interest here (`0014`). The product should not stay silent about
why.

**The attestation gate.** Coverage blocks on **both** `UNMATCHED_PAYMENT` *and* `UNATTESTED`, which
makes "these are all my cards" a **blocking precondition** — not a checkbox buried in settings. An
unattested household gets refusals, not sweeps, on day one. This surface is load-bearing.

**Execution note:** `mobile/AGENTS.md` requires reading the versioned Expo docs
(`docs.expo.dev/versions/v57.0.0/`) **before** writing mobile code. Adding a navigation library is a
new dependency — justify it or hand-roll the tab switch.

**Test scenarios:** the worst-30-day panel renders the correct maximum from a fixture profile; a
growing-balance household renders the "sweep is not the answer" copy and **no** interest-avoided
figure; an unattested household sees the attestation prompt and the Decisions tab shows a refusal;
tab state survives a re-render and the Explain modal still opens.

**Verification:** `npm run typecheck` and `npm test` green; screens render against a fixture artifact.

Full detail: `docs/plans/2026-07-14-001-feat-card-spend-portfolio-reserve-plan.md` § U7.

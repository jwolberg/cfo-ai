---
id: "0025"
title: Mobile — the household switcher
type: feature
status: open
priority: medium
repo: cfo-ai
agentId: mobile-rn-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-16-001-feat-multi-tenant-persistence-plan.md
depends_on: ["0024"]
created: 2026-07-16
---

# Mobile — the household switcher

Implements **U7** of the plan. Single owner: `mobile-rn-agent`.

**This is the unit that delivers the actual ask** — *"seed it with mock data for a couple of
different customers so I can better understand the dashboards."* Everything before it is plumbing;
this is the part someone looks at.

**Depends on:** `0024` (the household-scoped read path).

**Files:** `mobile/src/api/client.ts`, `mobile/src/api/types.ts`, `mobile/src/screens/Dashboard.tsx`,
`mobile/src/screens/Spending.tsx`, `mobile/src/screens/ExplainModal.tsx`,
`mobile/src/components/`, tests alongside each

## What to build

A picker that lists `GET /households` and threads the selection through every call. Selection lives
in one place; screens read it rather than each holding their own.

The four archetypes need labels a human can tell apart at a glance — the point is *comparison*, so
"Household B" is a failure. Name them by what makes them different: pay cadence and card count.
`GET /households` returns the label (`0024`).

## Read `USERS.md` before choosing where the switcher goes

`USERS.md` names two audiences and says they "should not be conflated." Both matter here:

- **The Resfi customer** opens the app "to see whether the system did anything, and to be reassured
  if it didn't." They have **one** household. A switcher is not their feature.
- **The interviewer/reviewer** wants to see, "in under a minute, whether the product argument is
  backed by working code." The switcher is entirely for them.

`USERS.md` §2 also says: *"The demonstration **is** the product surface; a second, instrumented
'admin' view would undercut the point."* So **do not build an admin screen.** The switcher is a
demo affordance sitting lightly on the customer's surface — closer to an account picker than a
console. If it starts to look like a dashboard-of-dashboards, it has gone wrong.

## Acceptance criteria

- [ ] Switching household re-fetches and re-renders the decision feed and the summary numbers.
- [ ] **The Spending screen follows the switch.** It is a separate tab (`0016`) and is the easiest
      thing to leave pointing at a stale household — a bug that looks like working software.
- [ ] The explain modal and the assistant follow the switch. Asking "why not last Tuesday?" about
      household C must not answer about household A.
- [ ] Labels distinguish the archetypes by pay cadence and card count.
- [ ] Selection survives a tab change; it need not survive an app restart.
- [ ] Loading and error states per household — a slow or missing household must not render as an
      empty dashboard, which reads as "the engine did nothing" rather than "we failed to load."
- [ ] `npm test` and the typecheck clean.

## Watch for

Archetypes B/C/D may **refuse constantly** (see `0023`). A feed of nothing but refusals is the
honest output and it is what §9.3's cost looks like on a screen. **Render it well rather than
hiding it** — [`prd.md`](../prd.md) §2.1: "Days with no sweep are not failures; they are the feature
working." If the refusal feed looks broken, that is a copy and design problem, not a reason to
change the seed.

---
id: "0025"
title: Mobile — the household switcher
type: feature
status: done
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

- [x] Switching household re-fetches and re-renders the decision feed and the summary numbers.
      `householdId` is a dependency of the load callback, so the effect re-runs; the screen returns
      to `loading` first rather than holding the previous household's feed on screen while the next
      is in flight.
- [x] **The Spending screen follows the switch.** ✅ **Closed by `0031` (2026-07-16).**

      It shipped at `[~]`: `GET /spend` had no household in it — the one route `0024` could not
      move to Postgres — so the screen refused for three of the four households, in the product's
      own voice, rather than render the demo's spending under someone else's label. That refusal
      was the honest half of this AC, and it was pinned and mutation-tested.

      `0031` scoped the route and made it per-card, so the tab now follows the switch like every
      other screen and the refusal panel is deleted. The risk that remains is the *opposite* one —
      holding the previous household's figures on screen while the next request is in flight, which
      is the same "bug that looks like working software" this ticket named. The screen returns to
      `loading` on every switch, matching the feed's behaviour two ACs up, and it is pinned:
      `Spending.test.tsx::a switch clears the previous household before the new one arrives` and
      `App.test.tsx::the spending tab follows the switch, and asks for the household you picked`.
- [x] The explain modal and the assistant follow the switch. The household travels with both the
      narration fetch and every assistant turn, so the backend loads *that* household's window and
      hands the model nothing else — it cannot cite another household's figure rather than being
      asked not to. Switching also closes an open modal: it is open against a decision belonging to
      the household you just left.
- [x] Labels distinguish the archetypes by pay cadence and card count, and a test asserts they do —
      including that none of them says "Household B".
- [x] Selection survives a tab change (it lives in `App.tsx`, and both screens stay mounted); it
      does not survive a restart, which the ticket allows.
- [x] Loading and error states per household. The picker's own label fetch fails **quietly** — the
      list is chrome, and a red banner because a label did not load would be the picker reporting a
      fault the product does not have.
- [x] `npm test` (60) and the typecheck clean.

## Watch for

Archetypes B/C/D may **refuse constantly** (see `0023`). A feed of nothing but refusals is the
honest output and it is what §9.3's cost looks like on a screen. **Render it well rather than
hiding it** — [`prd.md`](../prd.md) §2.1: "Days with no sweep are not failures; they are the feature
working." If the refusal feed looks broken, that is a copy and design problem, not a reason to
change the seed.

---
id: "0061"
title: The harness runs through the payment leg — late, failed and returned transfers
type: feat
status: open
priority: medium
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-22-001-feat-proving-the-engine-harness-and-observability-plan.md
depends_on: ["0059"]
created: 2026-07-22
---

# The harness runs through the payment leg — late, failed and returned transfers

`backend/replay.py` grades a decision as though the money **moved, instantly, and successfully**. Real
transfers post late, fail outright, and return days after they appeared to settle. Each of those
changes the *next* day's snapshot, and therefore the next day's decision — and none of it is measured
anywhere in this repo.

That gap matters more than it looks. `grade()` is careful to apply the sweep itself so a replay cannot
flatter itself about breach risk. But it applies the sweep **on the day we decided** — the assumption
most likely to *find* a breach. A returned transfer is the opposite case: money we believed was gone
comes back, and the household's real trajectory diverges from both the projected and the graded one.

## What to build

A transfer-outcome model in the replay loop, fed back into the next day's snapshot:

    posts_same_day   — today's baseline assumption
    posts_late       — settles N days after the decision
    fails            — never leaves; the obligation is still outstanding
    returns          — settles, then reverses after ~3 days

Deterministic and scenario-driven like everything else in the rig — outcomes come from the scenario's
event schedule, not from sampling at replay time, so a run stays reproducible.

## What it must measure

- Does a returned or failed transfer **cause a breach**, and does the engine notice in time?
- Does the engine double-pay an obligation it believes was already settled?
- Does a late post make the next day's decision act on a balance that is not there?

## Acceptance criteria

- [ ] Transfer outcomes are part of the scenario schedule, not sampled at replay time.
- [ ] The next day's snapshot reflects the actual outcome, not the assumed one.
- [ ] A returned transfer that causes a breach is attributed correctly — it is *our* doing, so it must
      not be filed under the household's own overdraft.
- [ ] A failed transfer leaves the obligation outstanding and the engine re-decides it.
- [ ] The observability surface (`0060`) shows transfer outcome on the household timeline.

## Notes

- This is the first honest test of the transfer/Method work already built (`0038`–`0045`,
  `backend/method/`), which to date has been exercised as a transport, never as a thing that fails.
- Deliberately after `0059`/`0060`: without scenarios there is nothing to attach outcomes to, and
  without the surface there is no way to see what they did.

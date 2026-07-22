---
id: "0062"
title: A regression gate — licensed() refuses engine changes, not just dial settings
type: feat
status: open
priority: medium
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-22-001-feat-proving-the-engine-harness-and-observability-plan.md
depends_on: ["0059", "0060"]
created: 2026-07-22
---

# A regression gate — `licensed()` refuses engine changes, not just dial settings

`backend/calibrate.licensed()` decides whether a **spend-model dial setting** may ship, and it has
already earned its keep by refusing one. But an engine change — a new gate, a changed cap, a reworked
forecast — ships today on tests and judgement. The rig that can price a dial setting can price those
too, and does not.

## What to build

Golden-file the per-scenario **decision mix and metrics**, so any engine change produces a readable
diff instead of a vibe:

- The committed artifact from `0060` is the golden file — it is already deterministic and diffable.
- A check that regenerates it and fails when the numbers move without the golden being updated.
- `licensed()` extended to compare a *candidate engine* against the baseline on the same population,
  with the same rules it already applies to dial settings: breach rate no worse **on every shape**,
  sweep-caused overdrafts a hard veto, and the change must actually buy something back.

That last rule is the one that transfers least obviously and matters most. Both safety rules get
*easier* to pass the more the engine reserves, so a gate that only measures safety will happily
license an engine change that is strictly worse for the household than the one it replaces.

## Acceptance criteria

- [ ] Regenerating the artifact with no code change produces no diff.
- [ ] An engine change that shifts decision mix or metrics fails the check until the golden is
      updated deliberately.
- [ ] `licensed()` accepts a candidate-vs-baseline **engine** comparison, not only a dial sweep.
- [ ] A candidate that reduces breaches purely by refusing more is **not** licensed — the
      buys-something-back rule holds for engine changes too.
- [ ] The check runs in CI alongside the existing suites.

## Notes

- Deliberately last in the sequence: the gate is only meaningful once the scenarios are adversarial
  (`0059`) and the numbers are legible (`0060`). A regression gate over the current shape-only
  population would lock in a measurement that misses the events that matter.

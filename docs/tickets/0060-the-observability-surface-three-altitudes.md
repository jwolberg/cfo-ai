---
id: "0060"
title: The observability surface — day trace, household timeline, population panel
type: feat
status: open
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-22-001-feat-proving-the-engine-harness-and-observability-plan.md
depends_on: ["0059"]
created: 2026-07-22
---

# The observability surface — day trace, household timeline, population panel

`calibrate.report()` returns a **string**. That is the entire ability to observe this engine. You can
read a breach rate; you cannot watch the engine behave, cannot see which gate fired on a given day,
and cannot tell a healthy engine from one that has quietly stopped sweeping.

This is the piece that makes the proof rig legible — and it is the demo.

## Three altitudes

**Day trace.** One household, one day: inputs → projected low → which gates fired → caps applied →
target card → decision + reason codes → and the realized outcome once the future is known. The
engine's own reasoning rendered, not a log line.

> **Corrected 2026-07-22.** This said "`decide.py` already emits codes rather than sentences, so this
> is a rendering problem, not an instrumentation one." The first clause is true and the conclusion
> does not follow — see **Two instrumentation changes come first**, below. Scope this ticket with
> them included.

**Household timeline.** 90 days as a strip: decision per day (sweep amount, or refuse code), the
projected low and the realized low as two lines, cumulative swept, breaches marked. This is what you
eyeball to catch "it refuses forever" or "it swept hardest right before the tail" — failure shapes no
scalar metric surfaces.

**Population panel.** Per scenario and per dial setting: breach rate, sweep-caused overdrafts,
false-refusal cost, and the **decision mix** — % sweep / % refuse, broken out by reason code.

## Why decision mix is the load-bearing new metric

Every safety number the rig has today gets **better** the more the engine refuses. Breach rate falls,
sweep-caused overdrafts fall, and `false_refusal_cost` deliberately excludes deferral codes
(`replay.py`'s own reasoning: a deferral is not a permanent cost of conservatism). So an engine that
silently stopped sweeping altogether would pass every existing gate with perfect scores. Nothing in
the repo currently catches that. Decision mix is what catches it.

Measured on the current population (1,350 graded days, 2026-07-22) it reads **10.6% `SWEEP`**, 59.7%
`CADENCE_HOLD`, 20.0% `CARD_BEHAVIOR_UNKNOWN`, 9.7% `NO_SURPLUS` — against a 14.3% cadence ceiling.
So the metric's first act is to certify the engine is **alive**. Report it against cadence-eligible
days as well as raw days, or a healthy engine will look like it refuses 89% of the time.

## Two instrumentation changes come first

Neither is large. Both are invisible until you try to build the surface, and pricing this ticket as
pure rendering hides them.

**1. `Graded` drops the `Decision`.** `backend/replay.py:79` carries `day`, `outcome` and
`deferred: bool` — the reason codes never reach a caller, so decision mix *by code* cannot be
computed from a replay today. `replay()` has the decision in hand (`w.decision`); it just does not
pass it on. Contained, and unavoidable for the population panel.

**2. `decide.py` emits a `Reason` only for gates that fire**, and several paths `return` early. So
"every gate evaluated, including the ones that did not fire" cannot be rendered from what the engine
currently says — it requires the engine to report what it *considered*, not only what it concluded.
That is an edit to the most protected module in the repo, for an observability feature.

**Prefer the cheap form**: render the gates that fired, plus the inputs each non-firing gate would
have read, derived outside `decide.py`. That explains a decision without touching the decision path.
A true "every gate evaluated" trace is a separate ticket with its own risk, and should be argued on
its own rather than arriving as a line item under a reporting change.

## Shape

A script generating a **self-contained HTML artifact** into `docs/reports/`, committed — the same
discipline `docs/status.html` and `docs/decision-flow.html` already follow. No server, no build step,
diffable in review, and openable by someone who has never cloned the repo.

## Acceptance criteria

- [ ] One command regenerates the whole artifact from the scenario library, deterministically.
- [ ] Day trace explains the decision, not just its outcome: the gates that fired **and** the inputs
      the non-firing ones read. A trace showing only the winning gate cannot explain a decision.
      *(Satisfy this outside `decide.py` if at all possible — see the instrumentation section.)*
- [ ] `Graded` carries the decision, so decision mix by reason code is computable from a replay.
- [ ] Timeline plots projected vs realized low on the same axis, with breaches marked.
- [ ] Population panel reports decision mix by reason code, per scenario and per dial setting.
- [ ] A scenario whose engine never sweeps is **visibly** wrong in the panel, not silently green.
- [ ] The artifact is self-contained (no external CSS/JS/fonts) and committed under `docs/reports/`.
- [ ] Regenerating with no code change produces no diff (determinism, so the file is reviewable).

## Notes

- Depends on `0059` — the panel is only interesting across designed scenarios; over seeds alone it
  restates what `calibrate.report()` already prints.
- This is the demo artifact. Beats 1–3 of the plan's demo are literally this file.

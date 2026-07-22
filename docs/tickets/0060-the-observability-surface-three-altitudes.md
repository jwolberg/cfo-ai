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
engine's own reasoning rendered, not a log line. `decide.py` already emits codes rather than
sentences, so this is a rendering problem, not an instrumentation one.

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

## Shape

A script generating a **self-contained HTML artifact** into `docs/reports/`, committed — the same
discipline `docs/status.html` and `docs/decision-flow.html` already follow. No server, no build step,
diffable in review, and openable by someone who has never cloned the repo.

## Acceptance criteria

- [ ] One command regenerates the whole artifact from the scenario library, deterministically.
- [ ] Day trace shows every gate evaluated, including the ones that did **not** fire — a trace that
      only shows the winning gate cannot explain a decision.
- [ ] Timeline plots projected vs realized low on the same axis, with breaches marked.
- [ ] Population panel reports decision mix by reason code, per scenario and per dial setting.
- [ ] A scenario whose engine never sweeps is **visibly** wrong in the panel, not silently green.
- [ ] The artifact is self-contained (no external CSS/JS/fonts) and committed under `docs/reports/`.
- [ ] Regenerating with no code change produces no diff (determinism, so the file is reviewable).

## Notes

- Depends on `0059` — the panel is only interesting across designed scenarios; over seeds alone it
  restates what `calibrate.report()` already prints.
- This is the demo artifact. Beats 1–3 of the plan's demo are literally this file.

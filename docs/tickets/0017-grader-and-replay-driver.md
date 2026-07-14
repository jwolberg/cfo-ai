---
id: "0017"
title: Wire the grader and build the replay driver
type: feature
status: done
priority: medium
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-14-001-feat-card-spend-portfolio-reserve-plan.md
depends_on: []
created: 2026-07-14
---

# Wire the grader and build the replay driver

Implements **U8** of the plan. Single owner: `backend-python-agent`.

**Goal:** Build the measurement harness that would *license* the spend-model change.

> **This ticket changes no decision, and nothing else in this feature waits on it.** It is a
> prerequisite for the *later* spend-model change, not for the portfolio reserve. `depends_on` is
> deliberately empty and nothing depends on it in turn.

**Depends on:** none.

**Files:** `engine/outcome.py`, `backend/replay.py` (new), `tests/test_outcome.py`,
`tests/test_replay.py` (new)

**Why it exists.** `engine/outcome.py` exists and **nothing calls it** — the grader is unwired, and
there is no replay driver. `docs/learnings/2026-07-13-the-spend-model-over-reserves.md` documents a
**$400–970 over-reserve** in the forecast's spend model and is explicit about the sequencing:

> *"Do not fix it yet. Land the grader (#3) and the replay driver (#4) first. Ship the new spend
> model with its dial set to reproduce today's refusals — no behaviour change, no new risk. Loosen it
> only as far as the **measured** breach rate licenses."*

Until this ticket lands, **the harness that would license loosening the forecast does not exist.**
That is why `SpendProfile` ships as a structure and a dashboard, feeding no decision.

## Two traps, both already documented, both easy to walk into

**1. The grader applies the sweep itself.** `grade()` does not take "the realized balances" — it takes
the household's own daily movement **excluding anything we did**, and deducts the decision's sweep
itself. A replay that grades each decision against the household's *untouched* history never compounds
the effect of its own sweeps and systematically **understates** breach risk: it reports the tail we
would have had if we had never acted.

> That is the difference between a shadow-mode report and a shadow-mode lie.

The caller cannot forget to apply the sweep, **because the caller is not the one who applies it.**

**2. A refusal is not always a loss.** `false_refusal_cost` on a `CADENCE_HOLD` day is a **deferral** —
that money moves next week. A replay must partition on it before totalling, or it counts the same
dollars every day they sit.

**The same question now applies to this feature's new blocking codes.** A
`CARD_COVERAGE_INCOMPLETE` refusal is a deferral pending attestation, not a permanent cost, and must
be partitioned the same way. So is `CARD_BEHAVIOR_UNKNOWN` — it resolves itself once three cycles are
observed.

**Test scenarios:** a replay over a household produces a breach rate consistent with a hand-computed
expectation; `false_refusal_cost` on a `CADENCE_HOLD` day is not counted twice across consecutive
days; a `CARD_COVERAGE_INCOMPLETE` refusal is classified as a deferral, not a loss; grading a decision
without applying its own sweep is impossible by construction.

**Verification:** A breach rate can be measured on a generated population.

> **That number is the prerequisite for ever touching `daily_discretionary_high` — and nothing else
> in this plan depends on it.**

Full detail: `docs/plans/2026-07-14-001-feat-card-spend-portfolio-reserve-plan.md` § U8.

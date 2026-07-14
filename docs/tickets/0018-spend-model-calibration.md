---
id: "0018"
title: Ship the empirical spend model behind a measured dial
type: feature
status: done
priority: medium
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-14-001-feat-card-spend-portfolio-reserve-plan.md
depends_on: ["0017"]
created: 2026-07-14
---

# Ship the empirical spend model behind a measured dial

Implements **U9** of the plan. Single owner: `backend-python-agent`.

**Goal:** Build the empirical spend model, put it behind a dial, and let the population
measurement decide whether it may be turned on.

**Depends on:** `0017` — the grader and the replay driver are what make this measurable at all.

**Files:** `engine/models.py`, `engine/forecast.py`, `backend/precompute.py`, `backend/replay.py`,
`backend/calibrate.py` (new), `tests/test_calibrate.py` (new), `tests/test_precompute.py`

**Why it exists.** `docs/learnings/2026-07-13-the-spend-model-over-reserves.md` documents a
**$400–970 over-reserve** in `forecast.py`'s spend model, proposes replacing `30 × p90_daily` with
the household's own enumerated 30-day windows, and is explicit that this is the one change in the
engine that **loosens** — so it must be measured, never argued:

> *"Ship the new spend model with its dial set to reproduce today's refusals — no behaviour change,
> no new risk. Loosen it only as far as the **measured** breach rate licenses."*

`0017` built the grader. This ticket builds the model, the dial (`SPEND_QUANTILE`), and
`backend/calibrate.py` — the population harness that grades every dial setting against today's
behaviour and licenses at most one.

## Outcome: the measurement refused the swap

`SPEND_QUANTILE = None`. The structure ships **inert** — `forecast.py` falls back to
`daily_discretionary_high` and the engine behaves exactly as it did.

Reserving against the worst 30-day stretch a household has **ever actually had** (`q=1.0`) breaches
**19.8%** of days against today's **2.3%**, across 60 households and 4,320 graded days. The model
is not wrong, it is **starved**: it reads that worst-ever window off the 60–150 days of history the
engine has, which is 2–5 *independent* months. Given three years it breaches 3.7%, and on the
fat-tailed household — the one it is most dangerous for today — it breaches zero.

Write-up: `docs/learnings/2026-07-14-the-empirical-spend-model-is-not-a-drop-in.md`.

**Follow-up (new ticket, not this one):** gate the empirical model on *history length* rather than
on a quantile. It beats the incumbent for households with years of data. That is not a dial — it is
a second model with an eligibility rule.

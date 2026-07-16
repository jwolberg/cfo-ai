---
id: "0019"
title: The walk, unified — and the dial nothing reads
type: bug
status: open
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-16-001-feat-multi-tenant-persistence-plan.md
depends_on: []
created: 2026-07-16
---

# The walk, unified — and the dial nothing reads

Implements **U1** of the plan. Single owner: `backend-python-agent`.

**This is a defect fix, not a refactor for tidiness. It ships alone and needs no database.**
Read the plan's Problem Frame before writing a line of it.

**Depends on:** nothing.

**Files:** `backend/precompute.py`, `backend/replay.py`, `tests/test_precompute.py`,
`tests/test_replay.py`, `docs/decision-engine.md`

## The defect

`assemble_snapshot()`'s docstring (`backend/precompute.py:803-812`) says it is exported for
`backend/replay.py` **so the two do not drift.** They have drifted:

- `build()`'s loop does **not** call it — it constructs a `Snapshot` inline at
  `backend/precompute.py:943-973`.
- That inline `Snapshot` **never sets `spend_30d_high`**, so it falls back to the dataclass default
  `None` (`engine/models.py:633`).
- `assemble_snapshot()` **does** set it: `spend_30d_high=spend_30d_high(history, today,
  spend_quantile)` (`backend/precompute.py:845`).
- `build()` has **no `spend_quantile` parameter at all** (`backend/precompute.py:863-870`).
  `replay()` has one (`backend/replay.py:159`) and threads it through.
- `calibrate.measure(quantile)` (`backend/calibrate.py:136`) sweeps 9 candidate settings through
  `replay()` → `assemble_snapshot()`.

Harmless **today**, and only because `SPEND_QUANTILE = None` (`backend/precompute.py:123`): both
paths produce `None` and agree.

> **It stops being harmless the moment the dial moves — which is the entire purpose of
> `calibrate.py`.** That file exists to find the right setting for this dial. If it ever licenses
> one, it will have measured a forecast `build()` is structurally incapable of shipping. **The
> harness would be grading an engine that is not the one serving.**

This is [`decision-engine.md`](../decision-engine.md) §8.4's bug class exactly, and §6.6's
"a test fails if anyone moves it without a measurement" guards the **measurement**, not the
**wiring**.

## The second copy

The ledger/settlement bookkeeping is **copy-pasted character-for-character**:

```python
# backend/precompute.py:886-890        # backend/replay.py:200-202
if settled := sweeps.get(yesterday, ZERO):
    ledger.pay(settled)
    swept_cumulative += settled
```

Same for the household's own card payment (`precompute.py:892-894` vs `replay.py:204-205`),
`swept_this_week` (`901-910` vs `217-220`), and `days_since_last_sweep`/`last_sweep_amount`
(`912-920` vs `209`, `221-222`).

**So there are 2.5 copies of the walk. A seeder (`0023`) must not become the fourth.** That is why
this lands first.

## What to build

Extract the day-stepping loop from `build()` (`backend/precompute.py:882-996`) into a generator
yielding `(day, Snapshot, Decision, ledger_state)`. Three consumers drive it:

- **`build()`** — stays a thin wrapper, assembling `DayRecord`/`Summary`/`SpendSnapshot` on top
  (`precompute.py:997-1007`, `1010-1063`). **Gains a `spend_quantile` parameter.**
- **`replay()`** — drives the same generator and grades.
- **`seed()`** — arrives in `0023`. Not this ticket.

Delete the inline `Snapshot` at `943-973`. `assemble_snapshot()` becomes the only one, as its
docstring has always claimed.

`ledger_state` must carry what `build()` folds into `DayRecord` today and cannot re-derive from
`Snapshot`/`Decision` alone: checking balance, savings balance, buffer floor, debt balance/apr/id.

## ⚠️ The byte-identical test is the safety net. Do not regenerate it.

`tests/test_precompute.py:489-494` compares the committed `backend/data/decisions.json` against a
fresh `build()`:

```python
assert art.DEFAULT_PATH.read_text() == art.to_json(build())
```

**If it goes red, the refactor is wrong — not the file.** Regenerating it to match new output
deletes the only evidence this change preserved behavior. Over half of `tests/test_precompute.py`
(~262-776) calls `build()` and expects an `Artifact`; that is the point, and it is why `build()`
stays a wrapper rather than changing shape.

`tests/test_precompute.py:279-281` (determinism) stays green too.

## Acceptance criteria

- [ ] One generator; `build()`, `replay()` drive it. No inline `Snapshot` remains in
      `precompute.py`.
- [ ] `build()` accepts `spend_quantile` and threads it to `assemble_snapshot()`.
- [ ] The ledger/settlement bookkeeping exists **once**.
- [ ] `tests/test_precompute.py:489-494` green, **unregenerated**. `git diff` shows
      `backend/data/decisions.json` unchanged.
- [ ] **New test — the one that would have caught this:** for a given `(spec, seed, day)`,
      `build()`'s snapshot equals `replay()`'s. Asserted at `spend_quantile=None` **and** at a
      non-`None` setting. **The second case must fail on today's code** — verify it does by writing
      the test first.
- [ ] `docs/decision-engine.md` §6.6's dial note records that the wiring is now real. The dial stays
      `None`: this ticket fixes *whether the dial is connected*, and changes no forecast.
- [ ] `pytest` and `ruff` clean.

## Out of scope

Moving the dial. `SPEND_QUANTILE` stays `None` — §6.6 says the measurement refused the change, and
that refusal stands. This ticket makes the refusal *meaningful* by ensuring the thing measured is
the thing shipped.

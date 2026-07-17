---
id: "0032"
title: generate() is not prefix-stable — the harness has never graded the household that ships
type: bug
status: open
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/tickets/0019-unify-the-walk.md
depends_on: []
created: 2026-07-17
---

# `generate()` is not prefix-stable — the harness has never graded the household that ships

**Found by `0019` in July 2026. Filed now, which is late, and the lateness is part of the story:**
`docs/implementation-notes.md` says three separate times that this "needs its own ticket, its own
measurement, its own diff", and for three sessions the ticket did not exist. It was pinned in a test
and mentioned in `decision-engine.md` §6.6, both of which are ways of remembering something and
neither of which is a way of doing it.

**It is the last known-wrong thing in the repo with no home.**

## The defect

`sim.household.generate()` draws from **one RNG stream, in order** — payroll, then bills, then
discretionary — and the payroll and bill loops both run to `through = start + days - 1`. So the
number of draws taken *before* the spend loop depends on `days`, and every discretionary draw
shifts with it.

`generate()`'s own docstring is *"Deterministic in `(spec, start, days, seed)`"*. That is true, and
it is the trap: **`days` is part of the household's identity, not a window onto it.**
`(spec, seed, days=150)` and `(spec, seed, days=181)` are different households.

## The consequence, measured

`build()` walks `WARMUP_DAYS + SERVED_DAYS` = **150**. `replay()` generates
`total + HORIZON_DAYS + 1` = **181**, because it needs future days to grade against. Same spec,
same seed, same 90 served days:

| | build (150) | replay (181) |
|---|---|---|
| served days that differ | — | **7 of 90** |
| sweeps | 10 | 10 |
| total swept | $11,705.45 | $11,817.74 |

Seven days out of ninety, including one where the artifact refuses and the harness sweeps $28.37.
Every number plausible; none of them the same household.

**This is `0019`'s own thesis one level deeper.** `0019` was written because `calibrate.py` swept a
dial `build()` could not read — "the harness grading an engine that is not the one serving". The
engine is now unified. The *household* is not.

## What survives, and what does not

- **`calibrate.py`'s population statistics survive.** 20 arbitrary seeds per shape are still 20
  valid households drawn from the same generator, so **2.3% / 0-in-590 / ~$544,640.58** remain
  honest statements about a synthetic population. They are cited in `prd.md` §5.2/§5.3,
  `strategy.md` and `decision-engine.md`, and they do not move because of this bug.
- **Per-household claims tying a replay number to the shipped artifact do not.**
  `docs/plans/2026-07-14-001`'s **"6.9% breach on the demo household"** describes a household
  `backend/data/decisions.json` has never contained. That sentence is wrong today and has been
  since it was written.

## Why it was not fixed in `0019`, and why that reasoning has expired

Because the fix **regenerates the artifact and moves every measured number** — which is exactly the
change `0019`'s byte-identical test exists to refuse. That was right: `0019` was a refactor, and a
refactor that moves the numbers has proved nothing.

It has expired because the artifact has since been regenerated anyway, deliberately, in `0030` —
and the technique for doing that honestly now exists and is proven. **Hash the decisions before the
change; compare after.** In `0030` the shape moved and the decisions did not, and that was provable.
Here the decisions *will* move, and the equivalent proof is the inverse: show exactly which days
moved and why, and re-report the population rather than quietly re-baselining it.

## The fix

**Give each draw stream its own RNG**, so a longer window appends draws rather than shifting them:
payroll, bills, and discretionary each seeded from `(seed, stream)` and each drawing in day order.
A prefix of days then produces a prefix of draws, which is the property the name promises.

Per-day seeding (`Random(hash(seed, day))`) is the more robust variant and changes every draw; the
per-stream fix is smaller and sufficient. Either way the artifact regenerates — decide which on the
measurement, not on taste.

**Do not** "fix" it by making `replay()` generate 150 days. It needs the horizon to grade against,
and truncating the future to match the artifact would trade a real defect for a silent one.

## Acceptance criteria

- [ ] `generate()` is prefix-stable: for `N < M`, the transactions of
      `generate(spec, start, N, seed)` are a prefix of `generate(spec, start, M, seed)`.
      Asserted directly, not implied.
- [ ] **`TestGenerateIsNotPrefixStable` is deleted** — the whole class, per its own docstring's
      instruction. It exists to fail on the day this is fixed. Do not edit it to pass.
- [ ] `build()` and `replay()` demonstrably walk the **same** household. That is the point of the
      ticket, and `tests/test_precompute.py::test_build_and_replay_therefore_walk_different_households`
      is the test that inverts.
- [ ] The artifact regenerates. **Report which days moved and by how much** — the diff is the
      evidence, and "the decisions changed" is a sentence this repo does not accept without one.
- [ ] `python -m backend.calibrate` re-run. **The population's numbers are re-reported as a
      change**, not silently written over: `prd.md` §5.2/§5.3, `strategy.md`, and
      `decision-engine.md` all cite 2.3% / 0-in-590 / $544,640.58, and every one of them must say
      the population moved underneath them. `prd.md` §2.4 is a whole section about a number that
      survived because nobody re-argued it.
- [ ] **The guardrail is a hard gate: 0 sweep-caused overdrafts, or it does not ship.** `prd.md`
      §5.2 outranks everything else here, and this changes what every household did.
- [ ] `docs/plans/2026-07-14-001`'s "6.9% breach on the demo household" is **corrected**, not
      deleted — it is the clearest surviving example of the claim this bug invalidates.
- [ ] `decision-engine.md` §6.6's postscript comes out with the defect.
- [ ] `pytest` and `ruff` clean.

## Watch for

**Every seeded household changes**, so `backend/data/decisions.json`, the four archetypes in
Postgres, and `calibrate`'s 60-household population all move at once. That is a lot of numbers
moving in one diff, and the temptation will be to update the documents to match. **The measurement
is the deliverable; the code change is small.** If the new breach rate is worse, that is the
finding — the same rule `0017` and `0023` both landed on.

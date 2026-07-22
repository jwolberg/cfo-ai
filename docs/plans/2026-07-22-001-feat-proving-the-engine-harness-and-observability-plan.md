---
title: Proving the engine — a scenario harness, an observability surface, and the demo built on them
type: feat
status: draft
date: 2026-07-22
origin: "How do I prove this works without real customer data?" — the question the read-only demo plan
  failed to answer. Supersedes docs/plans/2026-07-21-001-demo-read-only-public-demo-plan.md.
tickets: 0059 (scenarios), 0060 (observability), 0061 (payment leg), 0062 (regression gate)
supersedes: docs/plans/2026-07-21-001-demo-read-only-public-demo-plan.md
---

# Proving the engine — a scenario harness, an observability surface, and the demo built on them

## Summary

The previous demo plan pitched the **read-only public deploy** — an access-control story. It is the
wrong headline. `docs/decision-engine.md` says so in its own opening:

> This is not a demo. It moves no money and talks to no bank. It exists to pin down the logic that
> decides whether moving money is safe — because **that decision, not the plumbing around it, is what
> the company lives or dies on.**

The demo has to be the decision, and the demo has to answer the only question that matters before
real money is behind it: **how would we know if the engine were wrong?** Today that question has a
good answer buried in code and no answer anyone can *look at*. This plan builds the looking.

It also absorbs the read-only-deploy work rather than discarding it: that becomes the surface the
harness's conclusions are shown *through*, and a supporting answer about safety, not the main event.

## What already exists (do not rebuild)

The proof rig is largely built, and it is better than the demo plan gave it credit for:

| Piece | What it already guarantees |
|---|---|
| `sim/household.py` | Deterministic synthetic households. Zero-inflated lognormal spend, *deliberately* not Gaussian, because the whole calibration question is about the tail. `as_of(day)` **slices** rather than regenerates — the structural guard against lookahead bias. |
| `engine/outcome.py` | Four deliberately-distinct facts per graded day: signed `projection_error`, `sweep_caused_overdraft`, `false_refusal_cost`, `interest_claimed`. |
| `backend/replay.py` | Walks a household day by day and grades every day it can *honestly* grade. `grade()` applies the sweep **itself**, so a caller cannot produce a flattering shadow report by forgetting to. |
| `backend/calibrate.py` | Grades a population across shapes, seeds and dial settings, with a `licensed()` rule that has already **refused a change everyone expected to ship**. |

This plan adds nothing to the grading semantics. It makes the rig **legible, adversarial, and
complete through the payment leg**.

## The three gaps

**1. You cannot look at it.** `calibrate.report()` returns a string. There is no per-day trace, no
timeline, no decision mix. You can read a breach rate; you cannot watch the engine behave — which is
why the current demo has nothing to show but a refusal.

**2. Scenarios are shapes, not events.** The rig varies spend distribution and seed. It does not vary
*what happens to a household*: income skipping a cycle, a surprise annual charge, a card appearing
mid-history, a statement landing late, a household that overdrafts on its own. Those are the days a
sweep engine earns or loses its keep.

> **Corrected 2026-07-22, by running it.** This gap originally read "nothing in the rig is designed
> to produce a **sweep**", offered as the reason the flagship demo opens on `card_behavior_unknown`.
> That is false, and the plan's own headline metric is what disproves it. Replaying the existing
> `SHAPES × SEEDS` population through `replay()`'s exact wiring — 1,350 graded days — gives a
> decision mix of **10.6% `SWEEP`** (143 days), 59.7% `CADENCE_HOLD`, 20.0% `CARD_BEHAVIOR_UNKNOWN`,
> 9.7% `NO_SURPLUS`. `DEMO_POLICY` sets `min_days_between_sweeps=7`, so the ceiling is **14.3%** and
> the engine is running at ~74% of it; on days it was both permitted and had surplus, it swept. This
> corroborates the seeder's own record (10 / 5 / 9 / 11 sweeps per 90 days, `DEPLOY.local.md` §3e).
>
> The true statement is narrower: the **linked Plaid demo household** opens on
> `card_behavior_unknown` because it has fewer than three observed cycles — a *data* condition, not
> a rig limitation. That changes the cost of fixing the demo's opening frame (see `0059`'s note): the
> synthetic archetypes already sweep, so re-seeding may not need the scenario library at all.
>
> The gap that survives is the real one, and it is still worth building: the rig varies **shapes,
> not events**, so no scenario exists that is *designed* to exercise a specific event and assert a
> specific class of response.

**3. The harness stops at the decision.** A decision to move $340 is graded as though the money moved,
instantly and successfully. Transfers post late, fail, and return, and each changes the *next* day's
decision. That signal is currently unmeasured.

## The build

### A — Scenario library (`0059`)

Named, parameterized, adversarial households layered on the existing generator. Not more seeds:
designed **events**, each carrying a stated expectation about the *class* of decision it should
produce, so the assertion is behavioural rather than "it did not crash".

    steady_sweeper            — should sweep on most days it is ALLOWED to (see the cadence note)
    income_skips_a_cycle      — the forecast's worst case, arriving on schedule
    surprise_annual_charge    — a tail event inside the horizon
    card_appears_midstream    — coverage changes under the engine's feet
    statement_lands_late      — the APR/obligation path with stale terms
    self_inflicted_overdraft  — the household breaches alone; the engine must not be blamed
    thin_history              — should defer, and must **stop** deferring at cycle 3

> **Express every sweep expectation against cadence-eligible days, never against all graded days.**
> `min_days_between_sweeps=7` caps the achievable rate at 14.3%, so "sweeps on a majority of graded
> days" is unsatisfiable by construction — the only way to pass it is to change the policy, which
> measures a different product than the one that ships. `CADENCE_HOLD` days are not the engine
> declining to act; they are a rule the user chose, doing what it was chosen for.

### B — Observability surface (`0060`) — the piece that does not exist, and the demo

Three altitudes, generated by a script into a self-contained HTML artifact under `docs/reports/`,
committed. Same discipline as `status.html`: diffable, shareable, demo-able with no server.

- **Day trace.** One household, one day: inputs → projected low → which gates fired → caps applied →
  target card → decision + reason codes → and the realized outcome once the future is known. The
  engine's own reasoning, not a log.
- **Household timeline.** 90 days as a strip: decision per day (sweep amount or refuse code),
  projected low and realized low as two lines, cumulative swept, breaches marked. What you eyeball to
  catch "it refuses forever" or "it swept hardest right before the tail".
- **Population panel.** Per scenario and per dial setting: breach rate, sweep-caused overdrafts,
  false-refusal cost, and the **decision mix** (% sweep / % refuse, by code).

**Decision mix is the load-bearing new metric.** Every safety number the rig has today gets *better*
the more the engine refuses, so a silently-dead engine that never sweeps passes all of them. Nothing
currently catches that. (Run today it reads 10.6% sweep against a 14.3% cadence ceiling — so the
metric's first act is to certify the engine is *alive*, not to condemn it.)

**This is not purely a rendering job, and pricing it as one is the plan's biggest hidden cost.** Two
instrumentation changes come first:

- **`Graded` drops the `Decision`.** `backend/replay.py:79` carries `outcome` and `deferred: bool`
  and nothing else, so the reason codes never reach a caller. Decision mix *by code* — the whole
  point — needs `replay()` to carry the decision through. Small, contained, and unavoidable.
- **`decide.py` emits a `Reason` only for gates that fire.** Several paths `return` early. So the
  day-trace requirement "show every gate evaluated, including the ones that did not" cannot be met
  by rendering what exists; it needs the engine to say what it considered, not only what it
  concluded. That means editing the one module this repo most protects, for an observability
  feature. **Prefer the cheaper form**: render the gates that fired plus the inputs each
  non-firing gate would have read, derived outside `decide.py`. If a real trace is wanted, it is its
  own ticket with its own risk, not a line item under a reporting change.

### C — The payment leg (`0061`)

Extend the replay past the decision: simulate transfer outcomes (posts same-day, posts late, fails,
returns after 3 days) and feed the result into the next day's snapshot. Then measure the thing that
matters — does a returned transfer cause a breach, and does the engine notice in time. This is also
the first honest test of the transfer work already built.

### D — Regression gate (`0062`)

Golden-file the per-scenario decision mix and metrics, so an engine change produces a readable diff
rather than a vibe. `licensed()` gets a second job: refusing **engine** changes, not only dial
settings.

## The demo (A + B are sufficient; C and D deepen it)

Roughly ten minutes, and the through-line is *how would I know if this were wrong?*

1. **A household that sweeps.** Timeline: projected low tracking realized low, sweeps landing, debt
   falling. The product working.
2. **A household that refuses, and exactly why.** Day trace showing which gate fired. Refusal as a
   designed behaviour with a reason, not an error.
3. **The population panel.** Decision mix visible, so it is demonstrably not a system that only ever
   says no — the objection the current demo invites.
4. **A returned transfer** (once `0061` lands) and the engine reacting to it.
5. **`licensed()` saying no — and, better, `licensed()` having been wrong.** Two beats, and the
   second is the stronger one. It did refuse a change its author wanted: nothing at or below
   `q=1.0` is licensed, and the dial still ships at `None`
   (`docs/learnings/2026-07-14-the-empirical-spend-model-is-not-a-drop-in.md`). But it *would have
   licensed a $1.08M regression* — its two original conditions were both monotone in reserve size,
   so tightening the dial far enough passed both, and `report()` would have announced that
   regression as a buy-back of `$-1,078,015.78` (`implementation-notes.md`, 2026-07-14). A third
   condition and a test now pin it. The pitch is not "my gate held" — it is *my own safety gate was
   wrong in a way that would have shipped, and measuring it is what caught it.*

**The honest limit, said out loud, unprompted:** synthetic data proves the engine is internally
consistent, behaves as specified across shapes and events, and that changes are comparable
apples-to-apples. It does **not** prove the spend distribution matches reality — which is exactly why
`calibrate.py` found the p90 model *starved* rather than wrong. The rig is what makes a small real
pilot safe to run; it is not a substitute for one.

## What happens to the read-only deploy work

It stays, demoted from headline to support, and it is where the harness's conclusions get shown:

- The public site (`0057`, lane C) is the surface a linked household's live decision renders on. Once
  `0059` exists, the demo household should be seeded from a scenario that **sweeps**, so the public
  demo stops opening on `card_behavior_unknown`.
- `0058` (the demo plane cannot create a real household) is fixed in code and **not yet deployed** —
  migration `0015` on Neon, then the API redeploy. **This is not a "whenever" chore and the Sequence
  section below is corrected to match.** The site is public and live *now*, with the escalation open,
  so the exposure is running whether or not anyone is being shown it. It is under an hour of work.
- The junk household `hh_2b15ef3a51b347e3bcdaa300820b9395` from the 2026-07-21 measurement pass is
  still in Neon and still in the public picker. **Delete it before running migration `0015`, not
  after.** `0015`'s backfill marks a user demo only if *every* membership is to a demo household, and
  that junk row is a non-demo household the demo viewer owns — migrate with it in place and the demo
  viewer is backfilled `false`, `current_real_user` never fires, and the fix ships **inert** on a
  deployment that looks fixed. Nothing in the deploy path re-asserts the flag: both seeders do
  (`backend/seed.py:230`, `scripts/seed_demo_household.py:101`), but `scripts/deploy_prod.sh` runs
  neither.
- The security story becomes a **60-second answer** if asked "is it secure?" — a hole was found in my
  own deployment by testing it, and the fix closed the class rather than the instance. A good answer
  to a question; never the headline.

## Sequence

**`0058` first** — cleanup, then `0015` on Neon, then the redeploy, then re-run the fail-closed check
against the deployed API (`scripts/verify_demo_readonly.sh`). It is short, it is prepared, and it is
the only item here with a live public exposure behind it. An earlier draft of this section filed it
under "whenever", which contradicted the section above; that has been resolved in favour of the
stricter reading.

Then `0059` → `0060` for the demo-able artifact, with `0061` and `0062` following. Two notes that
change the shape of that path:

- **Re-seeding the public demo household may not need `0059` at all.** The synthetic archetypes
  already sweep (measured above), so seeding the demo from one of them could fix the "opens on a
  refusal" problem in hours. Try that first; if it works, `0059` is no longer on the demo's critical
  path and becomes what it should be — a proof-rig investment, sequenced on its own merits.
- **`0060` carries an instrumentation change before any rendering** (`Graded` must carry the
  decision). Budget it as such.

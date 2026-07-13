---
id: 1
slug: shadow-mode-harness-foundations
anchor: SES-0001
title: "Shadow-mode harness foundations — synthetic households + interest model"
status: active
started: 2026-07-13T19:54:25Z
ended: null
goal: "Build the shadow-mode harness foundations: #1 synthetic household generator (sim/) and #2 engine/interest.py counterfactual amortization — two independent branches"
tickets: [1, 2]
branches: ["chore/backlog-and-session-0001", "feat/interest-counterfactual", "feat/synthetic-households"]
prs:
  - https://github.com/jwolberg/cfo-ai/pull/4
  - https://github.com/jwolberg/cfo-ai/pull/5
  - https://github.com/jwolberg/cfo-ai/pull/6
related_research: []
related_docs:
  - docs/prd.md
  - docs/strategy.md
  - docs/decision-engine.md
  - docs/implementation-notes.md
  - docs/learnings/2026-07-13-the-spend-model-over-reserves.md
prior_sessions: []
---

## [1] Goal

Build the two leaf dependencies of the shadow-mode harness ([`prd.md`](../../../docs/prd.md)
§8's sole **Now** item). Both are independent of each other and of everything else, so they
can be built and reviewed as two parallel branches.

**Done** looks like: two merged-ready PRs — a `sim/` package that generates deterministic
households with a known realized future, and an `engine/interest.py` that can compute
interest avoided against a counterfactual. Together they unblock #3 (the grader) and #4
(the replay driver), which is where the actual calibration number comes from.

## [2] Context & pointers

### [2.1] Tickets

**#1 — Synthetic household generator** (`.TerMinal/backlog/0001-synthetic-household-generator.md`)
`depends_on: []` · high · now

Deterministic `generate(spec, seed) -> History` in a new `sim/` package. Key ACs:
- Same `(spec, seed)` → byte-identical history, forever. Seed is an explicit input; no
  ambient RNG, no clock.
- Spec covers pay cadence (weekly/biweekly/semimonthly/monthly), net pay + variance, a
  recurring bill calendar with per-payee amount distributions, and a daily discretionary
  spend distribution that is **right-skewed and zero-inflated** (real spend is not Gaussian).
- Injectable shocks, individually toggleable: missed paycheck, one-off large expense, spend
  regime change, early bill.
- Money is `Decimal` via `engine.models.money()` — the generator must not be what introduces
  a float into the system.
- A `History` slices **as of** a given day, returning only what was knowable then. This is
  the seam #4 needs for point-in-time correctness.

**#2 — `engine/interest.py`** (`.TerMinal/backlog/0002-interest-counterfactual-amortization.md`)
`depends_on: []` · high · now

Counterfactual amortization + `interest_avoided(debt, sweep_amount, schedule) -> Decimal`.
Key ACs:
- Returns `None` when APR is `None` rather than guessing a rate — and a test asserts we
  never emit an invented interest number.
- Rounding convention applied once, stated in the module docstring. This number appears in
  the customer notification *and* in the KPI the company is graded on; those must never
  disagree.
- Hand-computed amortization reproduced to the cent.

### [2.2] Research & docs

- [`prd.md`](../../../docs/prd.md) **§8** — shadow mode is the only **Now** item and the
  load-bearing step: "a measured tail-risk number" before money moves.
- [`prd.md`](../../../docs/prd.md) **§5.1** — realized interest avoided is the primary KPI,
  measured against the user's own pre-signup trajectory. That trajectory is #2.
- [`prd.md`](../../../docs/prd.md) **§1** — *"that's $31 of interest you won't pay"* is
  already product copy and the engine cannot compute it.
- [`decision-engine.md`](../../../docs/decision-engine.md) **§6.1** — the thresholds are
  judgment, not evidence. Shadow mode is how they get set.
- [`decision-engine.md`](../../../docs/decision-engine.md) **§6.3** — Plaid frequently does
  not return APR. #2 must refuse to rank rather than guess, matching `_select_target`'s
  existing `APR_UNKNOWN` posture.
- [`strategy.md`](../../../docs/strategy.md) **§3** — calibration, not data, is what
  compounds. The error distribution is the asset. This session builds its foundations.

### [2.3] Prior sessions

None — this is session 0001.

### [2.4] Git/PR state

Clean `main` at `8597d3a`. No open PRs. Nothing in flight.

### [2.5] Repo constraints worth knowing before writing code

- **`pyproject.toml` declares `packages = ["engine"]`.** Adding `sim/` requires updating it,
  or `sim` won't install and the tests will import-error in CI.
- **Zero runtime dependencies.** The generator's randomness must come from stdlib `random`
  with an explicitly-seeded `Random` instance — not module-level `random.*`, not numpy. A
  module-level RNG is shared global state and would break the byte-identical-replay AC.
- **`money()` rejects floats** (`engine/models.py:28`). The generator will be sampling from
  continuous distributions, which produce floats — every sampled amount must be converted at
  the boundary (`money(str(round(x, 2)))` or via `Decimal(str(x))`), never passed through.
  Expect this to be the main friction in #1.
- **Ruff**: line-length 100, `E,F,I,B,UP,SIM`. Python ≥ 3.10.
- Tests live in `tests/`; 65 exist and run in ~0.3s. Keep it that way — the generator must
  not make the suite slow.

## [3] Checklist

### [3.1] Ticket #2 — `engine/interest.py` (start here; smaller, no new package)

- [x] write failing test: a known balance/APR/minimum reproduces a hand-computed
      amortization schedule to the cent
- [x] write failing test: `interest_avoided` returns `None` when `Debt.apr is None`
- [x] write failing test: a $0 sweep avoids $0; a sweep that clears the card avoids exactly
      the remaining interest
- [x] implement `engine/interest.py` to make them pass; document the rounding convention in
      the module docstring
- [x] decide how the interest claim reaches the user (new `ReasonCode` vs optional field on
      `Decision`) and record the choice in `docs/implementation-notes.md`
- [x] wire the claim into `engine/explain.py`; test that no interest number is rendered when
      APR is unknown
- [ ] run `pytest` + `ruff check`; open PR + link the url into ticket #2 `prs:`

### [3.2] Ticket #1 — `sim/` synthetic household generator

- [x] update `pyproject.toml` `packages` to include `sim`
- [x] write failing test: same `(spec, seed)` produces an identical `History` twice
- [x] write failing test: every money amount in a generated `History` is a `Decimal`, and a
      generated `History` round-trips into a valid `Snapshot`
- [x] write failing test: `History.as_of(day)` never exposes a transaction dated after `day`
- [x] implement the generator core (pay calendar, bill calendar, discretionary sampler) to
      make them pass
- [x] write failing tests for each shock (missed paycheck, large one-off, spend regime
      change, early bill), then implement them
- [ ] run `pytest` + `ruff check`; open PR + link the url into ticket #1 `prs:`

### [3.3] The falsification run (the reason #1 exists)

- [x] with the generator working, measure a household's empirical p99 of realized 30-day
      discretionary windows against the `30 × p90_daily` that `forecast.py:100` assumes today
- [x] write up the result in `docs/learnings/` **whichever way it comes out** — if the
      current spend model is not the binding constraint, that hypothesis dies here and the
      forecast rework in #4's design notes should be dropped

## [4] Log

- **2026-07-13** — Session opened. Tickets #1 and #2 set `in-progress`.
- **2026-07-13** — #2 committed on `feat/interest-counterfactual`. The TDD gate caught a spec
  deviation mid-flight (see [5.1]) — corrected before implementing, not after.
- **2026-07-13** — #1 committed (`dc80576`) on `feat/synthetic-households`. The falsification
  run **confirmed** the spend hypothesis rather than killing it; written up in `docs/learnings/`.
- **2026-07-13** — Housekeeping: `git add -A` had swept the tickets + this session doc into #2's
  commit, so the PR diff carried tracking state unrelated to `interest.py`. Extracted onto
  `chore/backlog-and-session-0001` off `main`; both feature branches are now pure code+docs and
  the three merge to `main` independently (disjoint file sets).
- **2026-07-13** — Suite: 65 → 123 tests, ~0.3s. `ruff check` + `ruff format --check` clean.
  Nothing pushed; no PRs open.
- **2026-07-13** — Docs updated (README + `decision-engine.md` [6.5]/[7]). A trial merge of all
  three branches caught a real conflict: both features appended to the *end* of
  `decision-engine.md`. Moved [6.5] up next to [6.1] — which it is the empirical answer to
  anyway — and re-verified all three merge clean. PRs #4, #5, #6 opened.

## [5] Decisions

### [5.1] The interest counterfactual is the household's own payments, not the card minimum

The ticket's ACs said "minimums only". `prd.md` §5.1 says *"the user's own pre-signup payment
trajectory"* — and the PRD wins. Users of this product already pay above the minimum (that is
*why* they hold idle cash), so measuring against the minimum credits our sweep with interest
they were never going to pay, inflating the one number the company is graded on. That is the
exact KPI failure §5.1 was written to ban.

`Debt` gained `observed_monthly_payment`; `minimum_payment` is **not an input to the interest
math at all**, and a test asserts changing it cannot move the claim by a cent. Surfaced to the
user mid-implementation and confirmed. Rationale in `docs/implementation-notes.md`.

### [5.2] The claim runs to payoff — and lands 7.6x above the PRD's own example

Confirmed by the user. A $300 sweep on a $9,000 card at 23.99% (household paying $400/mo)
avoids **$236.94**. Arithmetically right — the card clears in ~31 months, so $300 escapes ~2.6
years of compounding — but 7.6x `prd.md` §1's illustrative *"$220 → $31"*. One of the two is
wrong. Documented in `decision-engine.md` [7.2]. **Open product question — see [7].**

### [5.3] Interest reaches the user as a `ReasonCode`, not a field on `Decision`

`INTEREST_AVOIDED` joins the advisory group beside `IDLE_CASH_ELSEWHERE`. It inherits the whole
reasons architecture (explain renders it, the CI copy gate covers it, the LLM narrates *from* it
rather than paraphrasing a finished financial sentence) — and the load-bearing part: when no
honest claim exists, **no reason is emitted**. There is no code path anywhere that can render an
invented number. Absence is the mechanism, not a formatting rule.

### [5.4] The spend-model hypothesis was tested, and survived

The generator was built partly to **kill** my own 7σ claim from the opening of this session. It
didn't. The engine reserves more than the household's worst 30-day stretch in three years — for
every profile tested (3.4σ–7.2σ), over-reserving $400–$970 against a p99 month, versus a $750
default buffer. So it is very likely the binding constraint on most decisions the engine makes.

**Deliberately not fixed.** Fixing it loosens the forecast and buys bigger sweeps, the one
direction `decision-engine.md` §3 forbids without evidence. It waits for #3 and #4 — which is
the entire argument for building the harness before the forecast.

### [5.5] Tracking state lives on its own branch, not inside feature PRs

Tickets and the session doc are versioned with the code but are **not** part of a reviewable
code diff. Keeping them out means #2's PR is 686 lines of interest model rather than 1,120 lines
of interest model plus four tickets and a session log.

## [6] Outcomes

Both tickets implemented, tested, committed. Nothing pushed; no PRs open yet.

| Artifact | Ticket |
|---|---|
| `engine/interest.py` + `ReasonCode.INTEREST_AVOIDED` | #2 |
| `sim/` household generator + `History.as_of` (the point-in-time seam) | #1 |
| `tests/test_spend_model.py` — the falsification, pinned as characterization tests | #1 |
| `docs/learnings/2026-07-13-the-spend-model-over-reserves.md` | #1 |
| `docs/implementation-notes.md` — decisions [5.1]–[5.3] | #2 |
| `docs/decision-engine.md` [7] — the interest model documented | #2 |

Suite 65 → 123 tests, ~0.3s. Lint + format clean. **#3 and #4 are now unblocked.**

## [7] Follow-ups

- **Settle the interest-claim horizon** ([5.2]). The engine claims $236.94 where the PRD
  illustrates $31. A to-payoff claim is honest but leans on the household holding its payment
  behaviour for years, and a number that large invites exactly the disbelief this product cannot
  afford. **Needs a product decision before the figure is shown to any user.** File a ticket.
- **Fix the spend model** ([5.4]) — but only *after* #4 can measure the breach rate. The
  sequencing is the whole point of the harness, and doing it early would trade a measurable,
  safe error for an unmeasured, unsafe one.
- **`Debt.observed_monthly_payment` has no producer.** #2 consumes it; nothing estimates it from
  transaction history yet. `sim/` emits `CARD_PAYMENT` transactions so #3/#4 can derive it, but
  the real pipeline needs a real estimator — probably alongside the recurring-event detector
  (`decision-engine.md` §6.2).
- **`money()` silently quantizes a rate.** `money("0.2399")` is `0.24`. I hit this myself, and
  the existing test fixtures had been carrying it (harmless while APR was only ranked, wrong the
  moment it multiplies a balance). Consider a distinct `rate()` constructor so the type system
  makes it unrepresentable rather than commented.

## [8] Notes

Sequencing note: #2 is listed first in the checklist despite #1 being the lower id. #2 is
self-contained inside the existing `engine` package and has no packaging changes, so it is
the faster path to a green PR. #1 and #2 have no dependency on each other and can be worked
in either order or in parallel branches off `main`.

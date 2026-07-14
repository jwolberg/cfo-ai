---
id: "0012"
title: Derivation — cards, spend profile, coverage detector
type: feature
status: todo
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-14-001-feat-card-spend-portfolio-reserve-plan.md
depends_on: ["0010"]
created: 2026-07-14
---

# Derivation — cards, spend profile, coverage detector

Implements **U3** of the plan. Single owner: `backend-python-agent`.

**Goal:** Turn raw history into the new types. Pure derivation — decides nothing, changes no
behavior.

**Depends on:** `0010` (types).

**Files:** `backend/precompute.py`, `tests/test_precompute.py`

**Three derivations:**

**1. Card transactions → `Card`.** Reconstruct the statement/unbilled split from the cycle. When the
close date is unknown, **infer it from observed payment dates** (plan § [9.4]).

> **Two traps in that inference, both found in review.**
>
> **The date bias governs placement only — never the amount.** Biasing the close date early is
> conservative on the *date* axis and anti-conservative on the *dollar* axis: a statement that will
> truly close at $2,000 on day 25, inferred to close on day 15, gets reserved at whatever posted by
> day 15 — say $1,200. The date lands safely inside the horizon while the figure is understated by
> $800. Always compute the amount against the *true* accrual window.
>
> **`observed_monthly_payment` must be derived from observed payment events, never from inferred
> cycle boundaries.** It is payments ÷ cycles, and it feeds the REVOLVER reserve directly. An early
> bias that invents *more, shorter* cycles divides the same payments across a bigger denominator and
> pulls the figure **down** — shrinking the very reserve term the bias was meant to protect.

When the inferred cycle is uncertain, assume the obligation lands **inside** the horizon. Inference
may reserve early (a smaller sweep — safe); it may never reserve late (an overdraft — forbidden). A
card with no observed payments at all yields `PaymentBehavior.UNKNOWN` and blocks.

**2. 12 months of history → `SpendProfile`.** Non-parametric by construction: enumerate the
household's own overlapping 30-day windows and read the quantile straight off them.
`docs/learnings/2026-07-13-the-spend-model-over-reserves.md` is explicit about why — real spend is
zero-inflated and right-skewed, and a parametric `mu + z·sigma·√t` would reintroduce the same class
of error the current model has (*"variance grows with √t, and this model grows it with t"*).

**This feeds no decision.** It is a structure and a dashboard. See `0017`.

**3. The unmatched-payment detector.** Scan the funding account for recurring, card-shaped outflows
(issuer merchant strings, monthly cadence, amount ≥ a plausible minimum) that map to **no connected
card**. Deterministic, runs off data we already have, and **fails toward refusal**.

**Behavior classification:** `UNKNOWN` below 3 observed cycles. A transactor carrying a balance for
**two consecutive** cycles reclassifies to `REVOLVER` (one cycle does not — that tolerates a one-off
late payment). Going the other way requires the full **three clean cycles**. The asymmetry is
deliberate: being slow to grant a grace period costs a smaller sweep; being quick to grant one means
under-reserving someone who owes the whole statement.

**Test scenarios:** see the plan's U3 section — a payment to `CHASE CARD SVC` with no connected Chase
card trips the detector; the same payment *with* a matching card does not; a single card-shaped
transfer does not (recurrence is required); an ambiguous inferred close date places the due date
inside the horizon, never outside; `rolling_30d_*` reproduces a hand-computed worst window.

**Verification:** Derivation is pure and total on the demo household. No decision changes yet.

Full detail: `docs/plans/2026-07-14-001-feat-card-spend-portfolio-reserve-plan.md` § U3.

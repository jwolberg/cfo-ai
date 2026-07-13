# Implementation notes

Running log of decisions made *during* implementation that the spec/PRD/ticket did not
settle, deviations from the plan, and tradeoffs accepted. Written for human review.

---

## 2026-07-13 — Ticket #2, `engine/interest.py`

### The accrual model: daily accrual, monthly posting

Real revolving cards accrue on the **average daily balance** and post at statement close.
A textbook monthly-periodic-rate amortization is simpler but cannot express *when in the
cycle* a payment landed — and this product sweeps **daily**, so the timing of a sweep
within a cycle is precisely the thing we're being paid to get right. A monthly model would
value a sweep on day 2 and a sweep on day 29 identically, which is wrong in the direction
of over-claiming.

So: interest accrues each day at `apr / 365` on the outstanding principal, with **no
intra-cycle compounding** (matching the average-daily-balance method), and is posted to the
balance — quantized to cents — at each statement close. Payments apply on their date,
before that day's accrual.

Rounding happens at exactly one place: statement close, via `money()`, which is
`ROUND_HALF_EVEN` (the existing repo-wide convention, `engine/models.py:33`). Not changing
it here — a second rounding mode in a money codebase is how the customer notification and
the KPI end up disagreeing by a cent.

### The counterfactual is the user's own payment trajectory, NOT the card minimum

**This corrects a spec deviation I introduced and the test suite caught.** My first pass used
"minimums only" as the counterfactual, which is what the ticket's ACs said. `prd.md` §5.1
says something different and more demanding: interest avoided is *"measured against a stated
counterfactual (**the user's own pre-signup payment trajectory**)."*

The difference is not cosmetic. Users of this product already pay **more** than the minimum —
that is *why* they have idle cash sitting in checking. Crediting our sweep with interest they
were never going to pay anyway inflates the headline number, and §5.1 exists precisely to ban
a KPI that improves when the engine merely becomes more optimistic.

So `Debt` gains `observed_monthly_payment: Decimal | None` — what the household was actually
paying — and the interest model takes the counterfactual payment as an **explicit argument**
rather than assuming one. `minimum_payment` is no longer an input to the interest math at all,
and there is a test asserting that changing it does not move the interest claim by a cent.

When `observed_monthly_payment` is `None` we make **no claim**. We do not fall back to the
minimum: that is exactly the flattering assumption we just rejected. In practice this costs
nothing, because `INSUFFICIENT_HISTORY` already refuses to sweep below 60 days of history —
by the time we are allowed to move money at all, we have seen two payment cycles.

*(Decision surfaced to the user mid-implementation and confirmed 2026-07-13.)*

### The claim runs to payoff, and does not assume we keep sweeping

Interest avoided is computed over the **life of the debt**, not a bounded window. Under the
minimums-only counterfactual this was untenable — a $9,000 card at 24% with a $180 minimum
amortizes at ~$2.50/month and takes ~50 years, so the "interest avoided" by a single sweep was
an enormous, unverifiable number hypersensitive to an assumption we already knew was wrong.
Against the user's *real* trajectory that pathology disappears: the same household paying
$400/month clears the card in ~40 months, and the payoff horizon is human-scale.

The with-sweep path applies **this one sweep** and then assumes the user's ordinary payments
continue. It does **not** assume we go on sweeping every day — we do not book credit for
actions we have not taken.

A household whose own payments do not cover their accruing interest has **no payoff**, and
therefore no interest-avoided figure. That is not an edge case to smooth over; it is a
household drowning, and the model says so by raising rather than returning a number.

### A debt that does not amortize raises, it does not return a number

If the minimum payment never covers the accruing interest (possible with
`minimum_payment = 0`, which `Debt` permits), the balance grows without bound and there is
no payoff. The module raises rather than returning a capped or `None` figure — a silent
sentinel here becomes a garbage interest claim downstream. Bad data fails loudly at the
boundary, per `decision-engine.md` §3.

### Interest reaches the user as a `ReasonCode`, not a field on `Decision`

Two options were live: an optional `interest_avoided` field on `Decision`, or a new
`ReasonCode`. Chose **`ReasonCode.INTEREST_AVOIDED`** (in the existing *Advisory — true,
and worth saying, but not why we acted* group, alongside `IDLE_CASH_ELSEWHERE`).

It needs no change to `Decision`'s shape, it inherits the whole reasons architecture for
free — `explain.py` renders it, `test_every_reason_code_has_copy` covers it, the LLM
narrates *from* it rather than being handed a finished financial sentence — and when the
APR is unknown the reason is simply **not emitted**, so there is no code path that can
render an invented number. The absence of the claim is structural, not a formatting rule.

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

---

## 2026-07-13 — Ticket #3, `engine/outcome.py`

### The grader applies the sweep itself; the caller cannot forget

`grade()` does not take "the realized balances". It takes `Realized` — the household's own
daily cash movement, **excluding anything we did** — and deducts the decision's sweep itself.

A replay that grades each decision against the household's *untouched* history never compounds
the effect of its own sweeps, and systematically **understates** breach risk: it reports the
tail we would have had if we had never acted. That is the difference between a shadow-mode
report and a shadow-mode lie, and it is far too load-bearing to leave to a caller's diligence.
So the caller cannot forget to apply the sweep, because the caller is not the one who applies it.

`settles_after_days` defaults to **0** — our money is assumed to leave the day we decided, not
when ACH would plausibly have posted. That is the assumption most likely to *find* a breach
rather than excuse one. A grader that flatters itself is worse than no grader, because it turns
an unknown risk into a false sense of a known one.

### Two bugs the grader found in itself, and both would have shipped

**1. `false_refusal_cost` was blaming us for the user's own cap.** The first draft computed it
as `hindsight_safe - swept`. On a real household that read **$7,129** — but the engine had swept
$300 because the user's `max_sweep` is $300. We were not being conservative; we were being
*obedient*. `prd.md` §5.3 says **our** cost of conservatism, and a guardrail the user chose is
not ours. Left as written, the metric would have been dominated by policy caps and the forecast
signal it exists to carry would have been buried in noise.

Fixed by exporting `decide.would_sweep(snapshot, low)` — *what `decide()` would have moved if
the projected low had been the realized one*, obeying every cap exactly as it does in
production. `false_refusal_cost` is now `should_have_swept - swept`: our forecast error, priced,
and nothing else. This also meant extracting `untouchable()` and `apply_caps()` out of `decide()`
so both modules share **one** definition; a second copy of that arithmetic would drift, and the
day it drifted the calibration numbers would start describing an engine that never shipped.

**2. `projection_error` was absorbing the size of our own sweep.** It compared
`Decision.projected_low_balance` against the **post-sweep** realized low. But
`projected_low_balance` is a projection of the household's *pre-sweep* trajectory — the sweep is
computed *from* it, not included *in* it. So a big sweep produced a hugely negative "error":
−$5,694 on a household the engine had forecast almost perfectly.

That would have poisoned the exact distribution `strategy.md` §3 calls the only asset that
compounds, and poisoned it *worst on the days we swept hardest*. Now measured against
`realized_low_unswept`, and there is a test asserting the size of our own sweep cannot move it.

The two corrected metrics now satisfy an exact identity when no cap binds:
**`false_refusal_cost == projection_error`.** Pinned as a test — if they ever drift apart under
an uncapped policy, one of them has a bug.

### `sweep_caused_overdraft` is a different fact from `overdrafted`

`prd.md` §5.2 says "**sweep-caused** overdraft rate" and means it. A household that goes below
zero on its own is not our doing, and folding those in would make the guardrail metric
meaningless — it would be dominated by lives we never touched. So the grader reports both, and
`sweep_caused_overdraft` is true only when `realized_low < 0 <= realized_low_unswept`: we are
liable for the difference we made, not for their circumstances.

### The finding: the per-sweep cap currently **masks** the spend-model bug

Under the default $300 cap, [6.5]'s over-reservation costs the user *nothing measurable* — the
cap binds long before the forecast error does, `false_refusal_cost` reads **$0.00**, and a
shadow-mode report run at these caps would pronounce the engine healthy.

The bug is **latent, not absent**. Lift the cap on the same household, same day, same realized
future, and it prices out at **$867.13** immediately. Which means it starts costing real money
precisely when the ceiling is raised — and `prd.md` §8's *"Then: raise the ceiling as
calibration proves out"* is exactly the plan to raise it. **The moment the company starts
trusting its calibration is the moment this bug begins to bite.**

Implication for #4, written into a test so it cannot be forgotten: the replay driver **must
sweep the cap across a range**. Run at production caps alone, it will measure a forecast error
of zero and issue a false clean bill of health.

---

## 2026-07-13 — Ticket #0001, `backend/precompute.py` + `backend/artifact.py`

### The finding: `BELOW_MIN_SWEEP` tells the user something untrue

**This is an engine bug, it is user-facing, and I have not fixed it — it is outside this
plan's scope (`engine/` is explicitly untouched), so it needs a decision.**

`engine/decide.py` builds up `cap_reasons` from `apply_caps()` — `PER_SWEEP_CAP`,
`WEEKLY_CAP`, `CLEARS_THE_CARD` — and then, if the capped amount lands under `MIN_SWEEP`,
**drops them all** and refuses with `BELOW_MIN_SWEEP` alone:

```python
amount, cap_reasons = apply_caps(snapshot, target, available)
reasons = [projection, *cap_reasons]
if amount < MIN_SWEEP:
    return _refuse(Reason(ReasonCode.BELOW_MIN_SWEEP, {"minimum": MIN_SWEEP}), low=low)
```

`explain.py` renders that as *"What's left is under $1.00, which isn't worth moving."* But
when the cause is an exhausted weekly cap, what is actually true is *"you have $2,000 spare
and you have already hit your $600 weekly limit."* The user is told they have no money. They
have plenty of money; they have no **headroom**. Those are different facts, and the refusal
reports the wrong one.

I found this because my first tuning pass produced a window where **65 of 90 days** carried
that message. The fix looks small — carry `cap_reasons` into the `BELOW_MIN_SWEEP` refusal —
but it changes the reasons attached to a decision in the tested core, so I have left the
engine alone and worked around it. Worth its own ticket.

**The workaround, and its cost.** `DEMO_POLICY`'s weekly cap is set wide enough
(`max_sweep=$400`, `max_weekly_sweep=$1600`) that the household's actual cash position, not
an exhausted cap, is what does the refusing. The served window now reads honestly: 35 sweeps,
55 refusals, and the refusals are almost all `NO_SURPLUS` ("your balance is heading for a low
of $X… there's nothing spare"). The cost is that `WEEKLY_CAP` no longer appears in the demo's
own data — it is covered by a unit test with a tighter policy instead
(`test_the_weekly_cap_binds_when_sweeps_stack_up`).

### Sweeps are subtracted from checking; `sim/` does not know we exist

The plan's sketch reads the checking balance straight from `History.balance_on(day)`. That
history is the household's realized life **without us in it** — it knows nothing of our
sweeps. Left as written, three months of daily sweeps would drain the card while the checking
balance sat untouched, and the engine would go on finding surplus that, in the world it had
just created, was already spent.

So the walk carries a running total of settled sweeps and subtracts it. This is what produces
the demo's actual shape: the balance falls toward the buffer, the surplus runs out, and the
engine starts refusing on `NO_SURPLUS`. That equilibrium **is** the product, and without this
correction the demo would not have shown it.

### The debt ledger: `outstanding = principal + unposted interest`

Mirrors `engine/interest.py` exactly, because the two must agree — that module prices what a
sweep *saves*, and a ledger that drifted from it would claim savings against a balance the
engine never believed in. Interest accrues daily on the **principal** at `apr/365` and posts
at statement close, so unposted interest earns no interest (the average-daily-balance method,
not compounding). The balance the engine sees is principal plus what has accrued, so it grows
every day — including a day with no payment and no sweep.

Payments come off **principal first**, overflowing into accrued interest only if they exceed
it. That overflow is not a rounding detail: `CLEARS_THE_CARD` sweeps exactly `outstanding`,
and a payment that retired only principal would strand the accrued interest — the card would
converge on a balance of a few cents it could never clear and the engine would refuse to
sweep forever. A test pins it (`test_paying_the_full_outstanding_clears_the_card`).

### Income variation is bucketed by 28 days, not by calendar month

`Snapshot.income_variation` is documented as the coefficient of variation of *monthly* income,
and the gate (`MAX_INCOME_VARIATION`) is 25%. This household is paid **biweekly** — so a
calendar month holds two paychecks, except the ~4 times a year it holds three. Bucketing by
calendar month scores that pure calendar artifact as a ~24% swing in income and comes within a
whisker of tripping `INCOME_TOO_VARIABLE`, refusing to serve a household whose pay is in fact
identical every fortnight.

28-day buckets are the honest measure of a biweekly earner's variability, and that is what the
walk computes. Flagging it because it is a deviation from how `Snapshot`'s own docstring
describes the field — and because the same trap is waiting for the real recurring-event
detector when it is built.

### The card payment is an ORDINARY event, not `DEBT_MINIMUM`

The household pays $450/month against a $280 minimum. Tagging the whole payment `DEBT_MINIMUM`
would make `forecast.py` skip all $450 of it (see `EventKind`) while `decide()` reserved only
the $280 — **under-counting $170 of real outflow**, which is the direction that ends in an
overdraft. Emitting it as `ORDINARY` at full value instead means the $280 minimum is reserved
on top of a payment that already includes it: an over-count of $280, which costs a slightly
smaller sweep and cannot hurt anyone. Wrong in the safe direction, deliberately.

### Demo spec: a $14,000 card, not $9,000

`tests/test_outcome.py`'s household helper (a $9,000 card) is the persona, and I started
there. But this household clears a $9,000 card almost exactly inside a 90-day window — the
artifact came out with a **$1.06** balance, which lands the demo on a "paid off" banner
instead of on the engine's actual daily work. $14,000 sits mid-band for the persona
(`USERS.md`: $8k–40k) and still has $3,452 outstanding at the end of the window.

The paid-off path is still built and tested (`test_a_card_paid_off_mid_window_keeps_being_served`),
it is simply not what the demo's own data does — which is what the plan asked for.

### Deliberately not modelled

The gates for stale balances, unhealthy connections, and in-flight sweeps are all about a live
Plaid connection this demo does not have. Rather than invent failures, the walk holds those
inputs healthy (fresh balance, healthy connection, nothing in flight) and lets the refusals
that *do* appear come from the household's real cash position. A sweep settles at the start of
the next day, which is why yesterday's sweep — not today's — is the one that lands on the
ledger.

### Follow-ups

- **Ticket needed:** carry `cap_reasons` into `decide()`'s `BELOW_MIN_SWEEP` refusal, so a
  cap-exhausted refusal says so. User-facing, small, in the tested core.
- The summary's `interest_avoided_total` sums the engine's own per-sweep `INTEREST_AVOIDED`
  claims across the window ($6,019 against $8,924 swept). Each claim is honest on its own
  terms — it is what `engine/interest.py` says that one sweep saved, assuming no further
  sweeps — but summing 35 of them is a slightly different number from "total interest avoided
  by the whole window," and the two are close but not identical. Fine for the demo; worth
  naming before it appears on a slide.

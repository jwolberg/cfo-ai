# PRD — Autonomous Debt Paydown

**Version:** v1 (2026-07-12). Supersedes [`archive/prd-v0-advice-only.md`](./archive/prd-v0-advice-only.md).
**Evidence base:** [`reviews/2026-07-12-prd-adversarial-review.md`](./reviews/2026-07-12-prd-adversarial-review.md)

---

## [0] What changed from v0, and why

**v0 proposed an advice-only product.** It showed the user a daily "safe extra payment," explained
it, and had the user make the payment themselves. Automation was deferred to Phase 3 on the theory
that trust must be earned before the system is allowed to move money.

**I killed it. The evidence says advice does not work.**

A field RCT on **>40,000 cardholders** (Guttman-Kenney, Adams, Hunt, Laibson, Stewart & Leary,
*AEJ: Economic Policy* 2025) removed the autopay-at-minimum default — choice architecture closely
analogous to a daily payment recommendation. It moved the proximate decision hard: the share paying
exactly the minimum fell from 36.9% to 9.6%. Then, after seven months, it produced **no reduction
in actual debt, spending, or borrowing costs** — and *increased* missed payments by crowding out
autopay. Separately, Medina & Pagel (NBER 28956) nudged 3.1M customers into saving ~4.9% more and
found **essentially no change in credit card debt**: the freed cash did not flow to paydown.

The causal chain in v0 was *information → better choice → less debt*. The chain breaks at the last
link. A better forecast produces a more precise number that people still do not act on.

**The correct conclusion is the opposite of v0's:** if knowing the number does not change behavior,
then the only thing that changes behavior is **the money actually moving**. Automation is not the
reward for earning trust. Automation is the product. Trust is the constraint you engineer around.

This PRD is that product.

---

## [1] The product

**We move the cash you do not need onto the debt that costs you the most — and we are right about
"do not need."**

The user connects their checking, savings, and cards once. From then on the system forecasts their
near-term cash position **every day**, decides what is genuinely surplus, and sweeps it to the
highest-cost balance. The user does nothing. They are told what happened and why.

**We watch daily. We move weekly.** Those are two different things and the distinction is
load-bearing — see [2.4]. This used to read "every day, we move the cash," which was inherited
unexamined from the v0 advice product ([0]) and never survived being measured.

> *"You had $220 sitting idle in checking, so I moved it to your card. That's $31 of interest you
> won't pay."*

The user-facing promise is not "we help you pay off debt faster." It is: **you no longer have to
choose between paying down debt and being safe.**

### [1.1] What the product actually sells

Not optimization. **The absence of a decision.**

The target user is not failing to pay down debt because they lack information — they can see the
balance and the APR. They are failing because every extra dollar sent to a card is a dollar that
might have been needed, and the cost of being wrong (an overdraft, a bounced rent check) is
immediate and humiliating while the benefit (interest saved) is invisible and deferred. So they
hold the buffer and eat the 24% APR. That is not irrationality; it is a rational response to
uncertainty they cannot price.

**We price it for them, and we absorb the residual.** That is the entire product.

---

## [2] The hard part is not the sweep. It is the refusal.

Moving money is a commodity — Plaid Transfer, Dwolla, and bill-pay providers all do it. Forecasting
is hard but tractable. **The defensible thing, and the thing that kills us if we get it wrong, is
knowing when not to act.**

Every sweep is a draw from a probability distribution. At scale, some non-zero fraction will
overdraft someone. The asymmetry is brutal: the upside of a correct sweep is a few dollars of
interest; the downside of a wrong one is a $35 fee, a missed rent payment, and a permanently lost
customer who tells everyone. **We cannot win this on expected value. We have to win it on the
tail.**

Therefore the following are product requirements, not engineering details:

### [2.1] The refusal rule is a first-class feature

The system must be able to decide **"today I move nothing."** Days with no sweep are not failures;
they are the feature working. Explicit refusal conditions:

- Forecast confidence below threshold.
- Any linked account stale, disconnected, or in `ITEM_LOGIN_REQUIRED`.
- Detected income irregularity (see [2.2]).
- A large or unusual pending debit not yet posted.
- A user-configured blackout window (e.g. the 7 days before rent).
- Any recent sweep still in flight and unsettled.
- **A sweep too recently made** — see [2.4].

### [2.2] High cash-flow variance is a disqualifying condition, not an edge case

The behavioral literature is clear that much co-holding of debt and idle cash is **rational
precaution** against volatile income and the risk of a credit line being cut (Telyukova 2013;
Fulford 2015; Druedahl & Jørgensen 2018). For a user whose income is genuinely lumpy, the buffer is
not inefficiency — it is the correct hedge, and sweeping it is *worse advice than doing nothing*.

The system must detect income variance and **refuse to serve users it cannot forecast safely**,
even at the cost of a smaller addressable market. Segment sizing should assume roughly **15–20% of
consumers**, not "millions" unqualified.

### [2.3] We eat the downside

If a sweep we initiated causes an overdraft, **we reimburse the fee, immediately, without the user
asking.** This is expensive, it is a real reserve line on the P&L, and it is the single most
important line in this document.

It converts a probabilistic promise into a guarantee, which is the only form of the promise that a
loss-averse user will accept. It also aligns the company against its own worst instinct: a
reimbursement line makes over-aggressive sweeping *cost us money directly*, so conservatism is
enforced by the income statement rather than by good intentions.

**Every scaled competitor in this category has ended up in front of a regulator** — Dave (FTC +
DOJ, 2024), Brigit ($18M FTC settlement) — and never for model inaccuracy. Always for consent,
fees, and harm. A visible, automatic, no-questions guarantee is our best defense against becoming
the third.

### [2.4] How often we act is itself a risk control

*(Added 2026-07-14.)*

[2] says we win this on the tail, not on expected value. **Every sweep is an independent draw from
that tail, so how many draws we take is a lever on risk as directly as how large any one of them
is** — and until now nobody had ever set it.

The system swept **daily**, and no document in this repo ever argued for it. It was a fossil of the
v0 advice product ([0]), whose output was a *notification* and which counted "daily engagement" as
a virtue. When advice-only was killed the rhythm survived and the payload changed underneath it. So
this document promised to win on the tail while taking three times as many draws on it, and
promised "the absence of a decision" ([1.1]) while making thirty of them a month. The engine could
not honour it in any case: it already refuses while a sweep is unsettled, so ACH settlement was
suppressing most of those days anyway.

Measured across the demo household with throughput held constant, daily sweeping bought about
**$36/yr** of interest timing over weekly. That is the entire economic case for it.

**Weigh it against the guarantee in [2.3], because that is what a draw actually costs us.** Daily
takes ~142 debits a year; weekly takes ~45. Against 97 extra draws at a $35 reimbursement, daily
pays for itself only if the **per-sweep overdraft probability is under ~1%**. And [2] says not to
make this bet on expected value at all: the true cost of an incident is not $35, it is "a $35 fee,
a missed rent payment, and a permanently lost customer who tells everyone." At a few hundred
dollars all-in, the breakeven falls to ~0.1%.

**That probability is no longer unmeasured — and it still does not settle the question.** [8]'s
harness now exists and has run against a synthetic population: **0 sweep-caused overdrafts in 590
sweeps.** Zero events is not zero risk. By the rule of three, that bounds the per-sweep rate at
**≤0.51%** with 95% confidence — which *rules out* the >1% world and sits **5× above** the ~0.1%
all-in breakeven it cannot exclude. It is also a bound on 60 simulated households, not on the
world.

So the trade is unchanged and better understood: **~$36/yr of the household's money to take a third
as many draws on a tail we can now bound but not yet size.** Cheap insurance, and the same posture
we take everywhere else — not a free lunch. Promoting that bound to a result is exactly the move
this document exists to forbid.

*(Two caveats on the numbers above. The ~$36/yr figure predates the payday double-count fix
described in [5.2], which moved 41 of the demo household's 90 projections; it has not been
re-derived. And an earlier draft claimed daily also cost ~$48/yr in ACH fees and that the two
"cancelled" — that figure was assumed, not sourced, and is wrong under most processor pricing. It
is corrected rather than deleted because it was the second plugged-in number in this investigation
to point the right way for the wrong reason.)*

**Sweeps are now spaced at least a week apart**, as a user policy value rather than a hardcoded
constant. The forecast still runs every day and a held day is still graded: the cadence limits what
we *do*, never what we *know*. Full measurement in
[`learnings/2026-07-14-the-cadence-was-inherited-not-chosen.md`](./learnings/2026-07-14-the-cadence-was-inherited-not-chosen.md);
the engine's contract is [`decision-engine.md`](./decision-engine.md) §9.

**Still wrong, and known:** a fixed weekly spacing is a proxy for what actually matters, which is
the household's own cash cycle — surplus appears when they are *paid*, not every seventh day. The
right rule decides once per pay cycle, which for a semimonthly earner is naturally twice a month
and for a monthly earner once. It needs the recurring-income detector we have not built.

---

## [3] The user

- Stable, **low-variance** income (a hard gate — see [2.2]).
- $8,000–40,000 revolving credit card debt at 20–30% APR.
- Persistently holds idle cash in checking well above their real short-term needs.
- Wants the debt gone; does not want to think about it.

They are not looking for a budgeting tool. They are looking for permission to stop worrying.

---

## [4] Scope

### [4.1] In

1. **Account connection** — checking, savings, cards, via Plaid (Transactions, Balance,
   Liabilities).
2. **Cash-flow forecast** — recurring income and obligation detection; a 30-day projected daily
   balance with a conservative downside estimate, not a point estimate.
3. **Sweep decision engine** — deterministic. Protect minimums → hold the buffer → forecast →
   allocate surplus to highest effective APR → **or refuse.**
4. **Money movement** — ACH debit from checking, payment to the card. See [6] — this is the
   hardest external dependency and there is **no universal "pay this card" API**.
5. **Explanation** — every sweep, and every refusal, is explained in plain language from the
   deterministic inputs that produced it.
6. **The guarantee** — automatic overdraft-fee reimbursement ([2.3]).
7. **Controls** — buffer floor, per-sweep and per-week caps, blackout windows, pause, disconnect.

### [4.2] Out

Budgeting. Investments. Credit scores. Lending (see [7.2]). Chat-based financial planning.
Subscription cancellation.

### [4.3] Explicitly deferred, and why

**We do not become the lender.** The "sweep everything, we'll lend it back in an emergency"
model is structurally what Tally was — and it adds an adverse-selection problem *we create
ourselves*, since our own sweeps are what leave the user thin. See [7.2].

---

## [5] Success metrics

### [5.1] Primary KPI — realized, never projected

**Realized interest avoided**, measured against a stated counterfactual (the user's own
pre-signup payment trajectory).

v0's KPI was "reduction in *projected* payoff duration" — a projection our own engine computes,
which improves if the engine merely becomes more optimistic. That metric would have paid us to
shrink the buffer. It is now banned from the dashboard.

### [5.2] The guardrail metric that outranks growth

**Sweep-caused overdraft rate.** Target: effectively zero. This is a **hard gate on the primary
KPI** — a period where interest avoided rises and the overdraft rate rises with it is a failure,
not a win. Reviewed independently of the growth team.

**This guardrail has now been breached once, and catching it is the argument for how we measure.**
`derive_cash_events` emitted *today's* events as future ones — but the balance is already
end-of-day, so on a payday the forecast counted the paycheck **twice**. A phantom $2,600 inflow, a
projected low thousands too high, and a sweep against money that was never there: the exact inverse
of [2.3]'s "money arrives late and small."

Across a 60-household population it caused **43 sweep-caused overdrafts**. **The single-household
demo reported zero** — whether the bug bites depends on the cash position on whichever paydays a
given seed happens to produce, and one seed never landed on one. It is fixed, and the population
now reports **0 in 590 sweeps**.

The lesson is a requirement, not an anecdote: **a guardrail measured on one household is not
measured.** This metric is only meaningful across a population, and any process that reports it
from a single account is reporting nothing.

### [5.3] Supporting

- Reimbursement cost per user per month (the price of our guarantee — watch this like a hawk).
- Forecast calibration: did the realized low balance fall inside the predicted interval, at the
  stated rate? **First measurement: a 2.3% breach rate** — days the realized low came in *below*
  our projection — across 4,320 graded days. That is the only number that may ever be traded for a
  bigger sweep, and [8.1] is what it licensed (and refused).
- Refusal rate, and the false-refusal rate (money we left idle that was genuinely safe to move —
  our cost of conservatism). **First measurement: ~$544K** across the same population, deferrals
  excluded. That figure is the prize for fixing the spend model — and the reason to fix it
  *properly* rather than quickly.
- Connected-account retention (the cleanest available trust signal; replaces v0's undefined "user
  trust score").

Every number above comes from a **synthetic** population whose spending our own simulator
generated. They measure the code, not the world. They are the first honest numbers this engine has
ever had about itself, and they are not yet evidence about households.
- Cash-flow variance of the served population (are we drifting into users we cannot forecast?).

---

## [6] Hard external dependencies — decide before building

1. **Money movement.** Card networks are not a repayment rail; each issuer controls acceptance.
   Realistic options are a bill-pay partner, a deep-link handoff, or an FBO/custodial account via a
   banking partner. **Custody brings materially more compliance and reconciliation burden — avoid
   for as long as possible.**

   **No rail is chosen, and nothing in the codebase assumes one.** `engine/` and `sim/` have zero
   dependencies; the engine emits a `Decision` and something else moves the money
   ([`decision-engine.md`](./decision-engine.md) [6.4]). Where processors are named anywhere in
   these docs, they are illustrations of a *pricing shape*, never a vendor commitment — §2 cites
   them to argue the rail is a commodity. Keep it that way: the ladder from renting origination →
   owning orchestration on an FBO → a direct ODFI relationship is a **cost and control** decision,
   not a capability one, and per-item cost only starts to matter at a volume we are nowhere near.

   **The trap to avoid** is letting a processor's fee schedule leak into a decision rule. A sweep
   cadence or a minimum-sweep floor tuned to someone's per-transaction price is a **risk parameter
   set by a vendor**, and it has to be re-tuned the day the rail changes. ([2.4] was very nearly
   argued this way and is not — it rests on the guarantee in [2.3], which is rail-agnostic.
   `MIN_SWEEP` in `engine/decide.py` is the one constant still making an implicit cost claim, and
   it is flagged as open.)

   **And note the tension with [7.1]:** if distribution is embedded/B2B2C — a bank, an issuer, an
   employer — then the *partner* very likely owns the rail, and owning it ourselves becomes moot.
   Owning the rail and embedding in an institution pull in opposite directions. [7.1] should
   therefore settle before any money is spent climbing that ladder.
2. **Plaid does not reliably return APR**, minimum payment, or statement date for many issuers.
   Our "interest avoided" number and our allocation ranking both depend on APR. Decide the
   fallback: user-entered, estimated with visibly reduced confidence, or refuse to rank.
3. **Regulatory.** Moving money is not a UI change. Reg E unauthorized-transaction liability, ACH
   authorization and NACHA rules, KYC, returns and NSF handling, consent revocation, disputes.
   GLBA applies from day one regardless. Plaid's own production-access review is a real launch
   gate.
4. **Data freshness gate.** Plaid webhooks are at-least-once and unordered; balances are
   institution-dependent; connections break routinely. Below a defined freshness bar, the system
   **refuses** ([2.1]) rather than sweeping against stale data.

---

## [7] The two open strategic questions

These are not engineering problems and this PRD does not pretend to answer them.

### [7.1] Distribution — the one that actually decides the outcome

Tally raised ~$172M, reached an $855M valuation, and shut down in August 2024. The founder's own
post-mortem: acquiring consumers one app download at a time cost more than partnering with
institutions. **CAC killed it, not the technology.** The market then split the assets — LendingClub
took the consumer app, **Pagaya took the B2B platform.**

A direct-to-consumer funnel for this product is the half that has already failed once. The
plausible answers are an embedded/B2B2C channel (a bank, a card issuer, an employer) or a
distribution insight not yet articulated. **This should be settled before headcount is spent on
growth.**

**It also settles [6.1], and whoever answers it should know that.** If the channel is embedded,
the partner institution very likely owns the money-movement rail — and the whole
rent-vs-own-the-rail ladder in [6.1] becomes moot before we ever climb it. Owning the rail and
embedding in an institution pull in opposite directions. So this question is not merely *first in
importance*, it is **first in sequence**: it forecloses an engineering decision that looks
independent of it, and money spent on rail ownership before this lands is money spent on an option
that distribution may simply delete. (This is why the rail is not a third question here. It is not
open — it is *downstream*.)

### [7.2] Monetization

Consumer fintech does not monetize by subscription at scale: MoneyLion's subscriptions are ~6–7% of
revenue; Chime has no subscription tier; Dave abandoned voluntary tipping for a mandatory fee in
Feb 2025. And commitment-device demand collapses from >50% uptake at free to single digits at
$0.25.

**"Profit only on progress"** — revenue as a share of interest genuinely avoided — is the most
interesting answer available and the one nobody has made work. It aligns perfectly with [2.3]'s
guarantee and with [5.1]'s realized KPI. It needs to be shown to close as a business, on a
spreadsheet, before it is a strategy.

---

## [8] Sequence

| | | |
|---|---|---|
| **Now** | Forecast + refusal engine, run in **shadow mode** | No money moves. Compute what we *would* have swept, then check against what actually happened. Measure the hypothetical overdraft rate before it can hurt anyone. This is the only honest way to earn the right to [2.3]. |
| **Next** | Autonomous sweep, small caps, full guarantee | Low per-sweep ceiling, aggressive refusal, reimbursement live from day one. |
| **Then** | Raise the ceiling as calibration proves out | The cap is a function of demonstrated forecast calibration, not of growth targets. |
| **Later** | Additional debt types; the post-payoff product | Note: success removes the user from the market. What they become to us afterward is unanswered. |

Shadow mode is the load-bearing step. It gives us the one thing no competitor in this category ever
had before switching on the money: **a measured tail-risk number.**

### [8.1] Where "Now" actually stands

The **machinery** of shadow mode is built and has run; the **shadow** has not. That distinction is
the whole of the current status, and collapsing it in either direction would be a lie:

- **Built and run.** The grader (`engine/outcome.py`) has a caller (`backend/replay.py`), and
  `backend/calibrate.py` grades a population at every setting of the spend model. It has produced
  the first numbers this engine has ever had about itself ([5.2], [5.3]), and it has already earned
  its keep twice: it caught the payday double-count that breached [5.2] forty-three times, and it
  **refused** a forecast change that everyone — including this document's own plan — expected to
  ship.
- **Not started.** None of it has touched a real household. No Plaid link, no linked accounts, no
  live balances. The population is synthetic, and its spending was generated by the same
  assumptions the engine forecasts with, so it can measure the *code* and cannot yet measure the
  *world*. Every threshold in the engine is still judgment.

**The refusal is the part worth reading.** The known over-reserve in the spend model — the engine
reserves more than the household has ever spent in any 30-day stretch — was fixed exactly as
planned, measured, and the measurement said **no**: the replacement breaches 19.8% of days against
today's 2.3%, because it reads "their worst month" off 2–5 independent months of history and the
worst of 3 months badly understates the worst of 36. So the over-reserve is **still shipped**, the
dial is off, and a test fails if anyone moves it without a measurement.

That is the loop doing its job. A harness that only ever ratifies the change you already wanted is
not a harness. See `docs/learnings/2026-07-14-the-empirical-spend-model-is-not-a-drop-in.md`.

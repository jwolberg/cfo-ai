# Strategy — Underwriting Certainty

**Version:** v1 (2026-07-12). Supersedes
[`archive/strategy-v0-trust-ladder.md`](./archive/strategy-v0-trust-ladder.md).
**Companion:** [`prd.md`](./prd.md) · **Evidence:**
[`reviews/2026-07-12-prd-adversarial-review.md`](./reviews/2026-07-12-prd-adversarial-review.md)

---

## [0] What changed from v0

v0 argued a **trust ladder**: start with recommendations, earn trust, then automate, then become a
bank. The wedge was debt; the moat was proprietary data.

Two findings broke it.

**The ladder's first rung does not hold weight.** A >40,000-person RCT (Guttman-Kenney, Adams,
Hunt, Laibson, Stewart & Leary, *AEJ: Econ Policy* 2025) shows that changing what people *know*
about their debt changes what they *choose* — and does not change what they *owe*. Seven months on:
no reduction in debt, spending, or borrowing costs. An advice product does not earn trust by
working, because it does not work.

**The data moat is not the moat.** Plaid-derived transaction data is available to every competitor
who asks. What compounds is not the data — it is **calibration**: knowing, from millions of
observed outcomes, how often a forecast at a stated confidence was actually right. That is a
different asset, and it is only accumulated by a system that *acts* and then observes what happened.

---

## [1] The thesis

**We do not sell debt optimization. We sell the removal of a decision the customer cannot make
well.**

A household with $12,000 on a card at 24% and $6,000 in checking is losing roughly $2,900 a year to
a fear they cannot price. They are not ignorant of the arithmetic. They hold the cash because the
cost of being wrong — an overdraft, a bounced rent check, a declined card at the pharmacy — is
immediate and humiliating, while the interest saved is invisible and deferred. Faced with an
unpriceable risk, holding cash is the *rational* response.

The literature backs this: co-holding is driven substantially by precaution against income
volatility and the risk of a credit line being cut without warning (Telyukova 2013; Fulford 2015;
Druedahl & Jørgensen 2018), not by simple irrationality.

**So the business is not information. It is insurance.** We price the uncertainty, act on it, and
absorb the residual ourselves. The product is a promise: *you will not be caught short, and you
will stop paying 24% for the privilege of worrying.*

## [2] Why this is a business and not a feature

A bank could do this. None will, and the reason is structural — it is the argument of McKinsey's
*Great Banking Transition*, and it cuts in our favor:

- The bank earns the 24%. Sweeping idle deposits onto the card destroys interest income *and*
  deposit balances. **We are asking the incumbent to shoot itself twice.**
- The economics of the balance sheet are already bad — 70% of banking's capital, 5–7% ROE, a
  cumulative *negative* $600B of economic profit. Value has migrated to transactions and
  distribution, which earn 12–23% ROE on a fraction of the capital.
- We are a transaction-and-distribution business with no balance sheet to defend. That is the
  entire reason this is buildable from outside.

## [3] What actually compounds

**Not the data. The calibration.**

Every sweep is a prediction ("this $220 is not needed") that reality grades within days. Over
millions of sweeps we learn the one thing no competitor can buy from Plaid: **the empirical
distribution of our own errors**, by user archetype, by pay cadence, by season.

That is what lets us move a larger amount at the same tail risk — and moving more at the same risk
is the whole product. A competitor starting today has the same transaction data and none of the
error history. **The asset is the graveyard of our own mistakes.**

This only accrues to a system that acts. An advice product never learns whether it was right,
because nothing happens.

### [3.1] The loop is closed, and the first thing it did was say no

Until this week, **this section was not even true of our own codebase.** The grader existed and
nothing called it — the engine was in precisely the state described above, acting without ever
finding out whether it was right.

It now has a caller, and a population to run against: 60 synthetic households, 4,320 graded days,
and the first error distribution this product has ever had. A **2.3%** breach rate, **0**
sweep-caused overdrafts in 590 sweeps, ~**$544K** of measured conservatism.

The instructive part is not the numbers, which are synthetic and small. It is what happened when we
used them. The forecast has a known, documented over-reserve — it reserves more than the household
has ever spent in any 30-day stretch — and the fix for it was obvious, elegant, and would have
bought back a large share of that $544K. **We built it, measured it, and the measurement refused
it**: the replacement breaches nearly nine times as often, because it estimates a household's worst
month from 2–5 independent months of their history and the worst of 3 months badly understates the
worst of 36.

So we shipped the model we know is wrong, with the fix behind a dial set to `None`.

That is the asset behaving exactly as this section claims it should. **The graveyard of our own
mistakes is only worth anything if it is allowed to overrule us** — and its first act was to
overrule a change we wanted, on evidence we would not have had a month ago. A calibration loop that
only ever ratifies the decision you had already made is not an asset; it is a rubber stamp with
extra steps.

## [4] Conservatism is the strategy, not the brake

The instinct is to treat the safety buffer as a constraint on growth. It is the opposite.

We are selling a promise to loss-averse people. The value of that promise is set entirely by its
worst outcome, not its average one. **One overdraft caused by our sweep destroys more value than a
hundred correct sweeps create** — the fee is $35, the trust loss is total, and the story travels.

Therefore:

- **The refusal is the feature.** A system that declines to act on ambiguous days is more valuable
  than one that squeezes out an extra $40.
- **We reimburse the fee when we are wrong** — automatically, unasked. This converts a probabilistic
  promise into a guarantee, which is the only form a loss-averse customer will accept.
- **The reimbursement line makes conservatism self-enforcing.** Over-aggressive sweeping now costs
  us money directly, on the income statement, rather than depending on anyone's restraint.

Every scaled player in this category has ended in front of a regulator — Dave (FTC + DOJ, 2024),
Brigit ($18M settlement) — and never for an inaccurate model. Always for consent, fees, and harm.
The guarantee is the product *and* the compliance strategy.

## [5] Competitive position

| | What they do |
|---|---|
| YNAB, Monarch, Copilot, Rocket Money | Tell you about your money. Passive. Requires the user to act. |
| Tally | Tried this with a lending balance sheet. Raised ~$172M. **Dead, 2024.** |
| Bright Money | Automates paydown. Alive. BBB 1.2/5 — complaints about unauthorized charges and overdrafts. |
| **Us** | Act, refuse when uncertain, and **pay for being wrong.** |

The differentiator is not that we act. Bright Money acts. **It is that we are accountable when we
are wrong**, and that accountability is what earns the right to act at all.

## [6] The two questions this strategy does not answer

Naming them is the point. Anyone who claims to have solved these is guessing.

### [6.1] Distribution

Tally died on customer acquisition cost — by its founder's own account, acquiring consumers one app
download at a time cost more than partnering with institutions. Its assets were then split:
LendingClub took the **consumer app**, Pagaya took the **B2B platform**. The market's verdict was
that the platform was the valuable half.

A direct-to-consumer funnel for this product has failed once already, expensively. The credible
alternatives are an embedded channel (card issuer, bank, employer, payroll provider) or an
acquisition insight not yet articulated. **This decides the company, and it should be settled before
a dollar is spent on growth.**

### [6.2] Monetization

Consumer fintech does not sustain subscriptions: MoneyLion's are ~6–7% of revenue; Chime has none;
Dave replaced voluntary tips with a mandatory fee in Feb 2025. Commitment-device demand collapses
from >50% at free to single digits at $0.25.

**"Profit only on progress"** — a share of interest genuinely avoided — is the answer that fits this
strategy exactly: it aligns with the guarantee ([4]), with the realized KPI, and with the promise.
It is also the answer nobody has yet made close as a business. It needs a spreadsheet before it
needs a slide.

## [7] North star

**Become the system that knows, better than anyone, how much cash a household actually needs
tomorrow — and is willing to be wrong in public and pay for it.**

Debt is where that capability pays off first. It is not the point.

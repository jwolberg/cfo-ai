# cfo-ai

Working repo for a product thesis: **autonomous debt paydown** — every day, move the cash
a household genuinely doesn't need onto the debt that costs them the most, and be right
about "doesn't need."


```
python3 -m pytest tests/ -q      # 26 tests, ~0.25s
```

---

## `engine/` — the decision

The only code here, and deliberately so. Given a frozen snapshot of a household's cash
position, decide whether it is safe to move money to a card — **and usually decide that
it is not.**

| File | What it does |
| --- | --- |
| `models.py` | Value types. Money is `Decimal`, dates are inputs, everything is frozen. |
| `forecast.py` | The conservative projection: *money arrives late and small, it leaves early and large.* |
| `decide.py` | The refusal gates, the buffer, the reserved minimums, the caps, the target card. |
| `../tests/test_decide.py` | 26 adversarial tests. **These are the spec.** |

**Why this and nothing else.** Moving money is a commodity (Plaid, Dwolla, bill-pay all
do it). Forecasting is hard but tractable. The thing that decides whether the company
lives is *knowing when not to act* — every sweep is a draw from a distribution, and the
downside of a wrong one ($35 fee, a bounced rent check, a customer gone forever) dwarfs
the upside of a right one (a few dollars of interest). You cannot win this on expected
value. You win it on the tail.

So the engine is deterministic — no clock, no network, no LLM, no randomness. The same
snapshot yields the same decision forever, which is what makes a sweep explainable to a
customer, auditable to a regulator, and replayable in a backtest after Plaid rewrites the
underlying history beneath you. The LLM narrates `Decision.reasons`. It never produces
them.

Most of the code is reasons to do nothing. That's the feature.

Design notes and the deliberately-unbuilt parts: [`docs/decision-engine.md`](docs/decision-engine.md).

---

## `docs/` — the thinking

### The live product argument

| Doc | Why it exists |
| --- | --- |
| [`prd.md`](docs/prd.md) | What we'd build. Autonomous from day one; refusal and an overdraft guarantee as the load-bearing features. |
| [`strategy.md`](docs/strategy.md) | Why it's a business. We sell insurance, not information — and what compounds is calibration, not data. |
| [`decision-engine.md`](docs/decision-engine.md) | How the engine decides, and the four things it knowingly assumes away. |
| [`tech-prep.md`](docs/tech-prep.md) | The subsystems below the snapshot: pending→posted reconciliation, internal-transfer detection, webhook idempotency, the ACH state machine, reproducibility, and how you test a forecaster with no real data. |

### The evidence

| Doc | Why it exists |
| --- | --- |
| [`reviews/2026-07-12-prd-adversarial-review.md`](docs/reviews/2026-07-12-prd-adversarial-review.md) | Seven independent reviewers against the original drafts — premise, product, feasibility, security, scope, coherence, and external prior art. This is the document that changed everything. |

Two findings did the damage, and both are sourced there:

- **A >40,000-person field RCT** (Guttman-Kenney, Adams, Hunt, Laibson, Stewart & Leary,
  *AEJ: Econ Policy* 2025) changed what cardholders *chose* and, seven months later, had
  changed nothing about what they *owed*. **Advice does not move debt.**
- **Tally** raised ~$172M, reached an $855M valuation, and shut down in August 2024 — on
  customer acquisition cost, by its founder's own account. LendingClub bought the consumer
  app; **Pagaya bought the B2B platform.**

### The archive

| Doc | Why it's still here |
| --- | --- |
| [`archive/prd-v0-advice-only.md`](docs/archive/prd-v0-advice-only.md) | The original: recommend a number, let the user pay it, automate later once trust is earned. |
| [`archive/strategy-v0-trust-ladder.md`](docs/archive/strategy-v0-trust-ladder.md) | The original wedge argument: debt → automation → autonomous banking. |

Kept, not deleted. The RCT says the first rung of that ladder holds no weight — an advice
product can't earn trust by working, because it doesn't work. **Automation isn't the
reward for trust; automation is the product, and trust is the constraint you engineer
around.** That reversal is the whole point, and it's only legible if you can see what it
reversed.

### The system around it

[`architecture.md`](docs/architecture.md) — what we'd build and in what order. Notable for
what it *doesn't* build: no Temporal, no Redis, no provider abstraction for providers that
don't exist. The load-bearing choice is an append-only decision log that stores each
decision's entire frozen input, which is what makes a sweep explainable, auditable, and
backtestable after Plaid rewrites history beneath it.

---

## Status

- Only `engine/` exists. `architecture.md` is intent, not description.
- Engine thresholds (`INCOME_CONFIDENCE_FLOOR`, `MAX_INCOME_VARIATION`,
  `MAX_BALANCE_AGE_DAYS`) are **judgment, not evidence**. The honest way to set them is
  shadow mode: run against real households, move nothing, measure how often the realized
  low balance fell below the projection, and set the thresholds from the observed tail.
- Distribution and monetization are **named as unanswered**, not solved. They're what
  killed Tally, and pretending otherwise would be the one thing that discredits the rest.

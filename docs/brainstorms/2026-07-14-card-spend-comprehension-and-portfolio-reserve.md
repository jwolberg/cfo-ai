# Card spend, portfolio reserve, and the spend-comprehension surface

**Date:** 2026-07-14 · **Status:** scope, not yet planned · **Feeds:** a plan doc, then tickets
**Touches:** `engine/models.py`, `engine/forecast.py`, `engine/decide.py`, `engine/interest.py`,
`sim/household.py`, `backend/precompute.py`, `mobile/`

**Fact-checked against the code on 2026-07-14.** Every structural claim below ([0.1]–[0.4], [2.x])
was verified line by line. Three citation errors were found and corrected: a wrong dollar figure in
[0.2] (copied from a stale `precompute.py` docstring, now also fixed in the code), a quote in [2.1]
attributed to `decision-engine.md` §2.4 that is really a `decide.py` comment, and a misquote of the
§3 rule ("bug", not "change") that turned out to matter — see [0.4].

---

## [0] What this actually is

The ask was "add credit-card transactions as an engine input, and build a spend dashboard."
Halfway through scoping it, the feature turned out to be the fix for a hole in the safety
core. This document scopes both, because they are the same work.

Three findings, all confirmed in the code:

**[0.1] No credit-card charge has ever been simulated.** `sim/household.py` has a `CardSpec`,
but its `balance` is a static input and its `payment` is a fixed monthly constant. Every
`TxnKind.DISCRETIONARY` transaction hits *checking*. The card is a thing that gets paid down
and never charged. Everything downstream — the grader, `tests/test_spend_model.py`'s
measurements, the entire calibration story — has therefore never seen a credit card used as a
payment instrument.

**[0.2] The engine's own safety claim about card payments is false, and `precompute.py` is
already compensating for it.** `EventKind.DEBT_MINIMUM`'s docstring (`engine/models.py:56`) says
skipping the card payment in the forecast is safe because "we under-sweep, nobody is overdrawn."
`backend/precompute.py:248` says the opposite and routes around it — it emits the card payment as
an `ORDINARY` outflow at its **full $450**, deliberately eating a **$280** double-count of the
minimum, because under-counting "ends in an overdraft."

> **Figures, checked.** `DEMO_SPEC` is a **$450** payment against a **$280** minimum
> (`backend/precompute.py:158-161`). Tagging the payment `DEBT_MINIMUM` would skip all $450 while
> reserving only $280 — a **$170** under-count, toward overdraft. `ORDINARY`-at-full-value instead
> reserves the $280 on top of a payment that already contains it: a **$280** over-count, toward a
> smaller sweep. Those are the real numbers (`docs/implementation-notes.md`).
> **`precompute.py`'s own module docstring is stale** — it still narrates "$400 against a $180
> minimum … under-counting $220," which matches no current spec value and predates the
> $9k→$14k / $280-minimum change. It should be corrected as part of U4; leaving it is how the
> wrong number propagates, which is exactly how it reached this document.

Both cannot be right. `precompute.py` is right; the `EventKind` docstring is wrong. It assumed
the detected payment *is* the minimum. `engine/interest.py`'s own premise refutes that in
writing: *"Users of this product already pay more than the minimum — that is why they have
idle cash."*

The workaround only stands up because the demo's payment is a hardcoded constant. **Once the
payment is determined by what was charged, there is no constant left to hardcode**, and the
workaround has nothing to stand on. That is this feature.

**[0.3] `engine/interest.py` models a card balance that only ever shrinks.** `total_interest()`
has no concept of new charges, and `_check_amortizing()` raises if the balance grows between
statement closes. A revolver charging $1,500/month to the card they are being swept against has
a balance that genuinely grows. The model would report a payoff date that never arrives, and
the interest-avoided figure — the number `prd.md` §5.1 says the company is graded on — is
overstated by construction.

### [0.4] The consequence that matters

`Snapshot.daily_discretionary_high` is a p90 of **checking-account** spend. Move that spend onto
a card and the series collapses toward zero. The forecast stops reserving for spend at all,
while the real obligation reappears a month later as a statement payment.

The documented $400–970 **over**-reserve (`learnings/2026-07-13-the-spend-model-over-reserves.md`)
inverts into an **under**-reserve. That is the one direction `decision-engine.md` §3 forbids. Its
rule reads: *"an upstream data **bug** must never buy a bigger sweep."*

Note the word, because this feature widens it. Connecting a real credit card is not a bug — it is
the system working *correctly*, ingesting *better* data, and buying a bigger sweep as a direct
result. The existing rule does not literally cover that case. It should: the reason a data bug may
not enlarge a sweep is that the sweep must never grow for a reason unrelated to the household
genuinely having more spare cash, and a channel shift satisfies that description exactly. **The
rule wants restating as "an upstream data change must never buy a bigger sweep"** — and this work
is the occasion to restate it.

---

## [1] A card is a debit with a due date

The whole model follows from one sentence: **a card charge is not a checking outflow — it is a
checking outflow scheduled for the due date of the statement it lands on.**

```
  charge posted ──► card ledger ──► statement CLOSE ──► grace (≥21d) ──► DUE ──► leaves checking
       day 3            unbilled          day 20                             ~day 45
       day 27           unbilled       next close (day 20+1mo)               ~day 75
```

Two charges three weeks apart land on **different statements** and leave checking **a month
apart**. The close date is the seam, and it is load-bearing: getting it wrong by one day moves an
entire month of spend across the 30-day horizon boundary, which is the difference between
reserving for it and never seeing it.

### [1.1] The grace period is not a detail — it decides the interest model

If the household pays the statement in full, new purchases carry a grace period and accrue **no
interest**. If they carry a balance, they **lose the grace period** and new purchases accrue
from the posting date.

So `PaymentBehavior` ([3.4]) is not a UX nicety. It changes the reserve *and* it changes whether
there is any interest to avoid at all.

---

## [2] The safety hole, precisely

### [2.1] Liability is portfolio-wide; the reserve is per-card

`decide()` ranks debts by APR and sweeps to the winner. `untouchable()` reserves the sum of
`minimum_payment` across debts due in the horizon.

Consider a household with a **24% card** (the target, $14k balance, $280 minimum) and an **18%
card** they charge $2,000/month to and pay in full. The engine sweeps to the 24% card. It
reserves the 18% card's minimum — perhaps $40. On the 20th, **$2,000 leaves checking**.

We will have done the optimal thing with money that was already spoken for.

The engine already refuses to rank cards when an APR is missing (`APR_UNKNOWN`,
`decision-engine.md` §2.4), and `engine/decide.py:175` gives the reason in as many words:
*"paying the wrong card looks exactly like working while quietly destroying the entire point."*
This is that same argument one level up, with a worse ending: **paying the right card while
starving a card we cannot see.** Optimality within an incomplete portfolio is not optimality. It
is an overdraft with a good explanation.

**Therefore: incomplete card coverage is a blocking refusal.** Not a warning, not a degraded
mode. The engine already knows how to decline to serve rather than serve badly
(`INCOME_TOO_VARIABLE`, `APR_UNKNOWN`); this is the same instinct applied to the thing that
actually kills people.

### [2.2] How we know coverage is incomplete

We cannot prove a negative. But we can do far better than assume:

1. **Attestation.** The user confirms, at onboarding, that these are all the cards they are
   liable for. Necessary, and nowhere near sufficient — people forget the store card.
2. **The unmatched-payment detector.** Scan the funding account for recurring, card-shaped
   outflows (issuer merchant strings, monthly cadence, amount ≥ a plausible minimum) that map to
   **no connected card**. A recurring $300 to `CHASE CARD SVC` with no Chase card connected is
   hard evidence of a liability we are not reserving against.

The detector is the real gate. It is deterministic, it runs off data we already have, and it
fails *toward* refusal.

### [2.3] The interest claim assumes a card nobody uses

Covered in [0.3]. Restated as a rule: **we may not claim to have avoided interest on a balance
trajectory we have modeled as falling when it is in fact rising.** For a household whose card
grows faster than they pay it, the honest output is not a smaller number — it is `None`, and a
sentence on the dashboard saying the sweep is not their problem. `interest.py` already has the
machinery for this (`_check_amortizing` raises; `claimable_interest_avoided` returns `None`); it
simply is not being fed the charges that would trigger it.

---

## [3] Data structures

All new types are frozen dataclasses with `__post_init__` validation, per
`decision-engine.md` §3 — *bad data is rejected, never smoothed over.*

### [3.1] Spend has a channel

```python
class SpendChannel(str, Enum):
    CASH = "cash"   # debit, ACH, cash — leaves the funding account the day it posts
    CARD = "card"   # lands on a card ledger — leaves checking at that statement's due date


class Recurrence(str, Enum):
    RECURRING = "recurring"  # same merchant, monthly cadence, stable amount — rent, Netflix
    VARIABLE = "variable"    # regular but lumpy — groceries, fuel
    ONE_OFF = "one_off"      # the tail — a flight, a vet bill
```

`SpendCategory` adopts **Plaid's Personal Finance Category** taxonomy rather than inventing one,
because that is what the real ingestion layer will hand us and a bespoke taxonomy would need a
lossy mapping on day one.

### [3.2] The card transaction

```python
@dataclass(frozen=True)
class CardTransaction:
    """One posted charge or credit on a card ledger.

    Signed like every other amount in the engine: **negative is money out.** A charge is
    negative even though it *increases* what you owe, because the alternative — flipping the
    sign because the balance happens to be stored as a positive liability — is exactly the
    class of bug `CashEvent.__post_init__` exists to catch. One rule, everywhere, or none.

    A charge is not a checking outflow. It is a checking outflow scheduled for the due date of
    the statement it lands on. Nothing in this codebase may treat it as same-day cash.
    """
    card_id: str
    posted_date: date
    amount: Decimal        # negative = a charge; positive = a refund or statement credit
    merchant: str
    category: SpendCategory
    recurrence: Recurrence
```

### [3.3] The statement cycle

```python
@dataclass(frozen=True)
class StatementCycle:
    """The seam that turns spend into an obligation with a date."""
    close_day_of_month: int
    grace_days: int   # close -> due. Reg Z requires >= 21 on cards that charge interest.

    def close_on_or_after(self, day: date) -> date: ...
    def due_for(self, close: date) -> date: ...
```

Month-length clamping is already solved in `interest.py:_statement_day()` — reuse it, do not
write a second one.

### [3.4] The card

```python
@dataclass(frozen=True)
class Card:
    card_id: str
    apr: Decimal | None
    cycle: StatementCycle

    # Already closed. Legally due on `statement_due_date`. A known fact, not a forecast.
    statement_balance: Decimal
    statement_due_date: date
    minimum_payment: Decimal

    # Posted since the last close. NOT yet due: these close on the next close date and come
    # due a cycle after that. This is the number that determines *next month's* bill, and it
    # is the reason card transactions are an engine input at all.
    unbilled_balance: Decimal
    next_close_date: date

    behavior: PaymentBehavior
    observed_monthly_payment: Decimal | None = None

    @property
    def interest_bearing_balance(self) -> Decimal:
        """A TRANSACTOR's unbilled charges accrue nothing — the grace period covers them.
        A REVOLVER has no grace period, so their unbilled charges accrue from the day they
        posted. See [1.1]."""
```

`Card` **replaces** `Debt`. Keeping both would mean two authoritative sources for the minimum
payment, which is the precise failure `EventKind` was invented to prevent.

### [3.5] Payment behavior — the load-bearing enum

```python
class PaymentBehavior(str, Enum):
    """How the household settles this card. Learned from >= 3 observed cycles.

    Load-bearing in two places at once, which is why it is a stored field and not an
    inference made separately in each:

    - **The reserve.** A TRANSACTOR owes the whole statement on the due date. Reserving their
      $40 minimum against a $2,000 statement under-reserves by $1,960 and overdraws them.
    - **The claim.** A TRANSACTOR pays no interest at all — the grace period already does what
      our sweep claims to do. Their `interest_avoided` is $0, not a number. Sweeping their cash
      onto a card they were going to clear anyway is a *prepayment*, not a saving, and taking a
      share of it (prd.md §7.2) would be charging for nothing.
    """
    TRANSACTOR = "transactor"      # clears the statement; holds a grace period
    REVOLVER = "revolver"          # carries a balance; pays a habitual amount above the minimum
    MINIMUM_ONLY = "minimum_only"  # pays the minimum; may never amortize
    UNKNOWN = "unknown"            # < 3 cycles observed. We refuse rather than guess.
```

### [3.6] The portfolio, and its coverage

```python
class CoverageState(str, Enum):
    COMPLETE = "complete"                    # attested, and no unmatched card-shaped outflows
    UNMATCHED_PAYMENT = "unmatched_payment"  # we see a payment to a card we cannot see
    UNATTESTED = "unattested"                # the user has not confirmed this is all of them


@dataclass(frozen=True)
class UnmatchedPayment:
    merchant: str
    typical_amount: Decimal
    day_of_month: int
    months_observed: int


@dataclass(frozen=True)
class CardPortfolio:
    """Every card the household is liable for — or an honest statement that we do not know.

    See [2.1]. Optimality within an incomplete portfolio is not optimality.
    """
    cards: tuple[Card, ...]
    coverage: CoverageState
    unmatched_card_payments: tuple[UnmatchedPayment, ...]
```

### [3.7] The spend profile — what "normal" is

```python
@dataclass(frozen=True)
class SpendProfile:
    """What normal looks like for this household, over 12 months, across every channel.

    Non-parametric by construction, and the 2026-07-13 learning is explicit about why: real
    spend is zero-inflated and right-skewed, sigma is a poor description of its tail, and a
    parametric `mu + z*sigma*sqrt(t)` would reintroduce the same class of error the current
    model has. So we enumerate the household's own overlapping 30-day windows and read the
    quantile straight off them.

    Deterministic (the 'sampling' is their own history, already in the Snapshot), uses their
    real skew and autocorrelation, and explainable in one sentence:
    *'your worst 30-day stretch last year was $2,231.'*
    """
    window_days: int                              # 365
    commitments: tuple[RecurringCommitment, ...]  # the floor they cannot avoid
    by_category: Mapping[SpendCategory, CategoryStats]

    # Every overlapping 30-day total in the trailing year, split by channel. The dashboard
    # renders these. The forecast will eventually read its quantile off them — but NOT in this
    # work. See [4.5].
    rolling_30d_cash: tuple[Decimal, ...]
    rolling_30d_card: tuple[Decimal, ...]


@dataclass(frozen=True)
class RecurringCommitment:
    merchant: str
    category: SpendCategory
    channel: SpendChannel
    typical_amount: Decimal
    amount_p90: Decimal
    day_of_month: int
    months_observed: int


@dataclass(frozen=True)
class CategoryStats:
    median_monthly: Decimal
    p90_monthly: Decimal
    worst_month: Decimal
    months_observed: int
```

### [3.8] Snapshot

```python
@dataclass(frozen=True)
class Snapshot:
    ...
    portfolio: CardPortfolio    # replaces `debts: tuple[Debt, ...]`
    spend: SpendProfile         # new

    # STILL the forecast's spend input in this work. Deliberately not removed. See [4.5].
    daily_discretionary_high: Decimal
```

---

## [4] Engine changes

### [4.1] The reserve becomes portfolio-wide and behavior-aware

```python
def obligation_in_horizon(card: Card, horizon_end: date) -> Decimal:
    """What this card will actually take out of checking inside the horizon.

    Not the minimum. The minimum is what the *issuer* will accept. The obligation is what the
    *household* will pay, and for two of the three behaviours those are very different numbers.
    """
    if card.statement_due_date > horizon_end:
        return ZERO
    match card.behavior:
        case TRANSACTOR:    return card.statement_balance                  # all of it
        case REVOLVER:      return max(card.minimum_payment,
                                       card.observed_monthly_payment or ZERO)
        case MINIMUM_ONLY:  return card.minimum_payment
        case UNKNOWN:       raise AssertionError("blocked upstream")       # see [4.2]
```

`untouchable()` sums this across **every** card in the portfolio. This is a **tightening** — it
reserves more, so it sweeps less. Tightening for a genuine obligation is always safe and needs
no calibration evidence, which is why it can ship now while [4.5] cannot.

### [4.2] New reason codes

| Code | Kind | Meaning |
|---|---|---|
| `CARD_COVERAGE_INCOMPLETE` | blocking | A card-shaped outflow maps to no connected card. We will not sweep into a liability we cannot see. |
| `CARD_BEHAVIOR_UNKNOWN` | blocking | Fewer than 3 observed cycles on a card. We do not know what it will take. |
| `STATEMENT_RESERVED` | projection | Why available cash is smaller than the balance suggests. |
| `UNBILLED_ACCRUING` | advisory | *"$1,240 charged this cycle — due Sep 20."* Requirement (b), surfaced. |
| `NO_INTEREST_TO_AVOID` | advisory | The target is a TRANSACTOR. We decline to claim a saving that the grace period already provides. |

### [4.3] The forecast finally gets to skip the card payment honestly

Rename `EventKind.DEBT_MINIMUM` → `EventKind.CARD_PAYMENT` and **rewrite its docstring**, which
currently asserts a safety property that is false ([0.2]).

With the reserve now covering the card's *actual* obligation rather than its minimum, the
forecast skipping card-payment events is correct **for the first time**. The consequence is
clean: `precompute.py`'s ORDINARY-at-full-amount workaround, and the $180 double-count it
knowingly eats, both **delete themselves**. The workaround disappears because the model
underneath it is finally right.

### [4.4] Interest admits that cards get charged

`total_interest()` gains new principal:

- New charges enter the balance on their **posting date**.
- **TRANSACTOR** → grace period holds; unbilled charges accrue nothing; `interest_avoided` is
  `$0` and `NO_INTEREST_TO_AVOID` is emitted instead of a claim.
- **REVOLVER** → no grace; charges accrue daily from posting, exactly like the existing model
  treats principal.
- If charges outrun payments, `_check_amortizing()` raises, `claimable_interest_avoided()`
  returns `None`, and **no claim is made** — which is already the correct behavior, and is the
  honest thing to say to a household whose card is growing.

That last case is a product insight, not just an engine one: for those households the sweep is
not the answer, and the dashboard should say so ([5.4]).

### [4.5] What deliberately does NOT change

**`daily_discretionary_high` stays exactly as it is, and the forecast keeps using it.**

`learnings/2026-07-13-the-spend-model-over-reserves.md` is explicit: *"Do not fix it yet. Land
the grader (#3) and the replay driver (#4) first. Ship the new spend model with its dial set to
reproduce today's refusals."*

`engine/outcome.py` exists but **nothing calls it** — the grader is unwired, and there is no
replay driver. The harness that would license loosening the forecast does not exist. So
`SpendProfile` lands as a **structure and a dashboard**, feeding no decision. Swapping the
forecast onto its empirical quantile is a separate, later, evidence-gated change.

The distinction that makes this coherent: **[4.1] tightens** (a real obligation we were not
reserving), and tightening is always safe. **[4.5] would loosen**, and loosening requires the
measurement. Shipping one without the other is not a compromise — it is the rule.

---

## [5] The spend-comprehension surface

**Navigation:** bottom tabs — `Decisions` | `Spending`. This retires `App.tsx`'s "no router"
stance deliberately: comprehension is now a co-equal surface, not a detail of a sweep.

### [5.1] "This cycle" — requirement (b), made visible

The headline is the thing the engine now depends on:

> ### $1,240 charged this cycle
> **Due September 20.** Closes in 6 days.
> *Last cycle at this point: $980.*

Below it: the two obligations, separated, because they are due a month apart.

| | Amount | Due |
|---|---|---|
| Statement (closed Aug 20) | $2,240 | **Sep 14** — inside the horizon, reserved |
| Unbilled (since Aug 20) | $1,240 | Oct 14 — outside the horizon, not yet reserved |

And the line that ties the dashboard to the engine: **"We're holding back $2,240 of your cash
for this."** The reserve stops being arbitrary.

### [5.2] "What normal looks like" — requirement (a)

Three strata, because they behave differently and a single "monthly spend" number hides all of
it:

1. **The floor** — `RecurringCommitment[]`. Rent, insurance, subscriptions. Merchant, amount,
   day, channel, months observed. Summed: *"Your fixed floor is $3,240/month."* This is the part
   they cannot flex.
2. **The band** — per-category median / p90 / worst month over 12 months. Groceries, fuel,
   dining. *"Groceries: usually $520, bad month $780, worst $910."*
3. **The tail** — one-offs. *"Four times last year you spent over $800 in a week."* The tail is
   what the buffer is actually for, and it is invisible in a monthly average.

### [5.3] "Your worst month" — the reserve made legible

A strip chart of all 335 overlapping 30-day totals from the trailing year, with p90 and the
worst marked.

> **Your worst 30-day stretch last year: $2,231.**
> We reserve against months like that one.

This is the single most valuable panel, and it is doing double duty by design: it is the exact
data structure that will eventually replace `daily_discretionary_high` ([4.5]), rendered a
release *before* it is trusted with a decision. Ship it as comprehension, watch it against real
households, and it earns its way into the forecast having already been looked at.

### [5.4] When the sweep is not the answer

If charges outrun payments ([4.4]), the dashboard says so plainly:

> **Your card grew $310 last month.** You charged $1,760 and paid $1,450. A sweep will not
> catch that up — the spending is the thing to change.

The engine already refuses to claim interest here. The product should not stay silent about why.

---

## [6] Simulator

`sim/household.py` must actually issue card charges, or none of this is tested:

- `SpendSpec` splits by channel: a `card_share` of discretionary spend routes to the card
  ledger, the remainder to checking.
- New `TxnKind.CARD_CHARGE` on a card ledger, distinct from checking transactions.
- The monthly card payment stops being a constant `CardSpec.payment` and becomes a **function of
  `PaymentBehavior` and the closed statement balance** — which is precisely the real-world
  behavior this whole feature exists to model.
- A second card, so the portfolio reserve ([2.1]) and coverage gate ([2.2]) have anything to
  bite on.

---

## [7] Sequence

| Unit | Work | Changes decisions? |
|---|---|---|
| **U1** | Types: `SpendChannel`, `CardTransaction`, `StatementCycle`, `Card`, `PaymentBehavior`, `CardPortfolio`, `SpendProfile`. Validation only. | No |
| **U2** | Simulator: card charges, statement cycles, behavior-driven payment, a second card. | Yes — see [8] |
| **U3** | Derivation: card txns → `Card`; 12mo history → `SpendProfile`; the unmatched-payment detector. | No |
| **U4** | Engine: portfolio reserve, new reason codes, `EventKind` rename, forecast skip. | **Yes — tightens** |
| **U5** | Interest: new charges, grace period, transactor zero-claim. | Claim only |
| **U6** | Backend: `GET /spend`. | No |
| **U7** | Mobile: bottom tabs + Spending screen. | No |
| **U8** | *(prerequisite for the later spend-model change, not for this)* Wire `engine/outcome.py`; build the replay driver. | No |

---

## [8] Risks, including one that will definitely happen

- **The committed artifact churns.** `tests/test_precompute.py`'s
  `test_the_committed_artifact_is_the_one_the_code_generates` will fail at U2 by design.
  Re-run `python -m backend.precompute` and read the decision diff deliberately — that diff *is*
  the safety change, and skimming it wastes the only chance to see it.
- **`tests/test_spend_model.py` will fail.** Its own docstring says that is the point: *"They are
  expected to fail if and when the spend model is fixed."* Re-derive the numbers; do not delete
  the tests.
- **The demo may go all-refuse.** [4.1] reserves strictly more than today. `precompute.py`'s
  `_assert_demo_is_worth_showing()` fails the build if the served window has no sweeps. This is a
  *predictable* build break, not a surprise, and the fix is to retune `DEMO_SPEC` — **not** to
  soften the reserve to make the demo look good. Worth stating out loud because the pressure to
  do the second thing will be real and will arrive at the worst moment.
- **Refusing on `UNATTESTED` coverage may be too strict for onboarding** — it could refuse every
  new user on day one. Open question [9.1].

---

## [9] Open questions

**[9.1]** Does `UNATTESTED` coverage block a sweep, or only `UNMATCHED_PAYMENT`? Blocking on both
is safest and may make onboarding unusable. Leaning: block on `UNMATCHED_PAYMENT` (hard evidence
of an unseen liability), and treat `UNATTESTED` as a one-time onboarding gate rather than a daily
refusal.

**[9.2]** Should the engine ever sweep to a `TRANSACTOR` card at all? It saves them nothing
([3.5]). Arguably a transactor card is not a target — it is only ever a *reserve*.

**[9.3]** A transactor who misses one payment loses their grace period and silently becomes a
revolver. How many cycles of evidence before we reclassify? Reclassifying too slowly means
claiming $0 interest for someone now paying 24%.

**[9.4]** Plaid does not reliably return statement close date (`prd.md` §6.2, the same gap that
makes APR unreliable). If we cannot see the close date, we cannot place the obligation on the
calendar. Does a missing close date become another refusal code, or do we infer the cycle from
observed payment dates?

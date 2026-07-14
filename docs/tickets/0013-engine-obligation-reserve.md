---
id: "0013"
title: Engine — the obligation reserve, the reason codes, the forecast
type: feature
status: todo
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-14-001-feat-card-spend-portfolio-reserve-plan.md
depends_on: ["0011", "0012"]
created: 2026-07-14
---

# Engine — the obligation reserve, the reason codes, the forecast

Implements **U4** of the plan. Single owner: `backend-python-agent`.

**This is the safety fix. It is the reason the feature exists.** Read the plan's Problem Frame and
High-Level Technical Design before writing a line of it.

**Depends on:** `0011` (simulator), `0012` (derivation).

**Files:** `engine/models.py`, `engine/decide.py`, `engine/forecast.py`, `engine/explain.py`,
`backend/precompute.py`, `docs/decision-engine.md`, `tests/test_decide.py`, `tests/test_explain.py`,
`tests/test_forecast.py`, `tests/test_precompute.py`

## The defect

`untouchable()` is **already portfolio-wide** — it sums across every debt due in the horizon. The
defect is narrower and worse:

> **It reserves each card's *minimum*. It should reserve each card's *obligation*.**

The minimum is what the **issuer** will accept. The obligation is what the **household** will pay,
and for two of three behaviours those are very different numbers. A household charging $2,000/month
to a second card and paying it in full has a $2,000 obligation and a $40 minimum. We reserve $40. On
the 20th, $2,000 leaves checking.

## ⚠️ The reserve must cover TWO statements, not one

**The first draft of this plan opened the very hole it exists to close. Do not re-open it.**

Today's reserve is a *rolling forecast*: `backend/precompute.py:436` recomputes
`minimum_due_date=_next_due(today, STATEMENT_DAY)` **fresh every single day**, so it reserves the
minimum on essentially every day of the cycle. `Card.statement_due_date` is the opposite — *"already
closed… a known fact, not a forecast."* Key the reserve off that alone and the moment the statement
is paid, the next has not closed, and the reserve drops to **$0** for roughly the last third of every
cycle.

```
obligation_in_horizon(card, horizon_end):
    total = 0

    # 1. The statement that has already closed. A known fact.
    if card.statement_due_date <= horizon_end:
        total += behavior_amount(card, card.statement_balance)

    # 2. The statement that has NOT closed yet, but will close AND come due
    #    inside the horizon. This is the term the naive design forgot.
    next_due = card.cycle.due_for(card.next_close_date)
    if next_due <= horizon_end:
        total += behavior_amount(card, projected_statement_at_close(card))

    return total


behavior_amount(card, statement):
    TRANSACTOR    -> statement                        (they clear it; the minimum is a lie)
    REVOLVER      -> max(minimum, observed monthly payment)
    MINIMUM_ONLY  -> minimum
    UNKNOWN       -> unreachable; blocked upstream by CARD_BEHAVIOR_UNKNOWN
```

With a 30-day horizon and ≥21-day grace, term 2 activates once the next close is within
`horizon − grace` days (≈9 days) — **precisely** the window term 1 leaves empty. The two terms tile
the cycle with no gap.

**Term 2's amount is a floor, not a forecast.** `unbilled_balance` is what has posted *so far*; more
will post before the close. Reserve `unbilled_balance` **plus projected charges through the close
date**, never the bare figure.

**And this is why it matters twice over:** the forecast skips the CARD_PAYMENT event
*unconditionally*, on the `EventKind` tag alone, regardless of whether `untouchable()` reserved that
dollar. In the window where a closed-statement-only reserve returns $0, **neither side accounts for
the obligation** — a double-miss, strictly worse than today's deliberate $280 over-count, in the one
direction `decision-engine.md` §3 forbids. **Term 2 is what makes the forecast-skip safe. Ship them
together or ship neither.**

## The rest of the unit

`_select_target()` **excludes `TRANSACTOR` cards from ranking** while `untouchable()` still reserves
against them. A transactor pays no interest — the grace period already does what our sweep claims to
do. Sweeping their cash onto a card they were going to clear anyway is a *prepayment, not a saving*,
and taking a share of it (`prd.md` §7.2, "profit only on progress") would be charging for nothing.

> **Retargeting can raise the swept amount, and that is not a reserve loosening.** `apply_caps()`
> applies `CLEARS_THE_CARD`, truncating the sweep to the target's balance. Excluding a small-balance
> transactor that would have won on APR means a larger-balance revolver is now the target, and that
> cap no longer binds — so the sweep can be *bigger* than today's for the same household on the same
> day. This is solvency-safe (`available` is unchanged, the reserve is strictly larger), but it is a
> real behavior change and the "tightens, never loosens" claim is about the **reserve**, not the
> swept amount.

**Five new reason codes:**

| Code | Kind | Meaning |
|---|---|---|
| `CARD_COVERAGE_INCOMPLETE` | blocking | A card-shaped outflow maps to no connected card, or the user has not attested. |
| `CARD_BEHAVIOR_UNKNOWN` | blocking | Fewer than 3 observed cycles. We do not know what this card will take. |
| `STATEMENT_RESERVED` | projection | Why available cash is smaller than the balance suggests. |
| `UNBILLED_ACCRUING` | advisory | *"$1,240 charged this cycle — due Sep 20."* |
| `NO_INTEREST_TO_AVOID` | advisory | The only cards are transactors. |

**Rename** `EventKind.DEBT_MINIMUM` → `EventKind.CARD_PAYMENT` and **rewrite its docstring**, which
currently asserts a safety property that is false. With the reserve covering the real obligation, the
forecast skipping card-payment events is correct **for the first time** — and `precompute.py`'s
ORDINARY-at-full-amount workaround, plus the $280 over-count it knowingly eats, **delete themselves.**

**Restate `decision-engine.md` §3's rule:** *"an upstream data **bug** must never buy a bigger
sweep"* → ***change***. Connecting a real card is not a bug; it is the system working correctly on
better data and buying a bigger sweep as a result. The rule does not literally cover the case this
feature creates.

## ⚠️ The reason codes are not done until both guard tests pass

Shipping a reason code without updating its downstream copy and guard has failed **three times** in
this repo (`docs/implementation-notes.md` documents all three; the most recent made **72 of 90 days**
unanswerable). Add each new code to `SAMPLES` in `tests/test_explain.py` **in the same commit**:

- `tests/test_explain.py::test_every_reason_code_has_copy`
- `tests/test_explain.py::test_the_engines_own_copy_passes_on_every_day`
- `tests/test_explain.py::test_a_refusal_does_not_read_like_an_error`

**This is a completion condition, not a follow-up.**

## The test that matters most

> **The tightening invariant, walked day by day across a full statement cycle.** The new reserve is
> never smaller than the old one, on **every single day of at least one complete cycle**, across
> generated households.

*Written this way because the naive design failed exactly here.* A closed-statement-only reserve
passes any spot-check taken in the first half of a cycle and drops to $0 for the last third. **A
sampled test would have reported green while the hole shipped.** Walk the cycle.

Other scenarios: the $2,000-transactor case (the test the whole feature exists for); reserve non-zero
on the day *after* the closed statement's due date; `UNMATCHED_PAYMENT` and `UNATTESTED` both block; a
2-cycle card blocks on `CARD_BEHAVIOR_UNKNOWN`; a transactor is never targeted even at the highest
APR; all-transactor portfolio emits `NO_INTEREST_TO_AVOID` and does not sweep.

## Expect the demo build to break

`_assert_demo_is_worth_showing()` fails the **build** if the served window has no sweeps, and this
unit reserves strictly more. That is a *predictable* break, not a surprise.

> **The fix is to retune `DEMO_SPEC` — never to soften the reserve to make the demo look good.**

Stated out loud because the pressure to do the second thing will be real and will arrive at the worst
possible moment. It has been flagged once before, in the cadence work, in almost these words.

**Verification:** Reserve provably ≥ today's on every generated household, every day of a full cycle.
`python -m backend.precompute` regenerates the artifact and **the decision diff is read
deliberately** — that diff *is* the safety change, and skimming it wastes the only chance to see it.

Full detail: `docs/plans/2026-07-14-001-feat-card-spend-portfolio-reserve-plan.md` § U4.

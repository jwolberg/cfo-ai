---
id: 5
title: "Settle the interest-claim horizon before the number reaches a user"
status: open
priority: high
horizon: now
hitl: true
type: feature
source: session-0001
created: 2026-07-13
updated: 2026-07-13
prs: []
refs: ["docs/decision-engine.md#7.2", "docs/prd.md#1", "docs/prd.md#5.1"]
depends_on: [2]
agent_id: 1000x-ai-engineer
agent_scope: global
agent_kind: classic
---

## Description

`engine/interest.py` (#2) computes interest avoided **to payoff**, against the household's own
payment trajectory. For a $300 sweep on a $9,000 card at 23.99% where the household pays
$400/month, it claims **$236.94**.

That is arithmetically correct — the card clears in ~31 months, so $300 of principal removed
today escapes ~2.6 years of compounding at 24%. It is also **7.6× the illustrative figure in
[`prd.md`](../../docs/prd.md) §1** (*"$220 → $31 of interest"*).

**One of the two is wrong, and it is a product decision, not an engineering one.** This is why
the ticket is `hitl: true`.

## The tension

A **to-payoff** claim is the honest total, but it leans on the household maintaining its
payment behaviour for ~31 months, is unverifiable against any document the user possesses, and
a number that large invites exactly the disbelief this product cannot afford. `strategy.md` §4
is explicit that the value of the promise is set by its worst outcome, and a claim the customer
does not believe is a promise that has already failed.

A **bounded-horizon** claim ("over the next 12 months this saves you $X") under-claims,
is checkable against a real statement, and would eliminate the non-amortizing error path
entirely.

There is also a **KPI** question hiding here. §5.1's *realized* interest avoided is measured
after the fact — but a to-payoff figure cannot be "realized" until the card is paid off, which
may be years. A bounded horizon is measurable inside a reporting period; a to-payoff figure is
a projection wearing a realized figure's clothes, and §5.1 bans exactly that
("a projection our own engine computes, which improves if the engine merely becomes more
optimistic").

## Acceptance criteria

- A decision is recorded in `docs/decisions/` as an ADR — this changes the meaning of the
  company's primary KPI and deserves a durable record, not a code comment.
- The customer-facing claim and the KPI use the **same** horizon, or the difference between
  them is explicit, named, and documented. They must never silently disagree.
- If a bounded horizon is chosen: `engine/interest.py` takes the horizon as an explicit
  argument (never a module default that a caller can forget), and `MAX_CYCLES` /
  `_check_amortizing` are re-examined — a bounded horizon makes the "does not amortize" error
  path unnecessary for the *claim*, though it may still be worth keeping as a signal about the
  household.
- `prd.md` §1's illustrative "$220 → $31" is either corrected to match the engine, or the
  engine is corrected to match it. **They cannot both stand.**
- The rendered copy states its assumption. It currently says *"if you keep your payments where
  they are"* — whatever horizon is chosen, the sentence must not imply more certainty than the
  model has.

## Design notes

Do not resolve this by picking whichever number looks better in a deck. The question is: **what
can we say to a customer that will still be true in eighteen months, and that they could check
if they wanted to?** That is the same standard the refusal gates are held to, and the interest
claim should not be held to a lower one.

Worth considering: report **both** — a bounded, checkable near-term figure in the notification
("$31 over the next year") and the to-payoff total in the dashboard, clearly labelled as a
projection contingent on their payment behaviour. That is more honest than either alone, but it
is two numbers and two chances to be misread. Decide deliberately.

---
id: "0014"
title: Interest — cards get charged
type: feature
status: todo
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-14-001-feat-card-spend-portfolio-reserve-plan.md
depends_on: ["0010", "0013"]
created: 2026-07-14
---

# Interest — cards get charged

Implements **U5** of the plan. Single owner: `backend-python-agent`.

**Goal:** `engine/interest.py` currently models a balance that only ever *shrinks*. `total_interest()`
has no concept of new charges, and `_check_amortizing()` raises if the balance grows between statement
closes. A revolver charging $1,500/month to the card they are being swept against has a balance that
genuinely grows — so today we would report a payoff date that never arrives, and overstate the
interest-avoided figure (the number `prd.md` §5.1 says the company is graded on) **by construction**.

**Depends on:** `0010` (types), `0013` (engine).

**Files:** `engine/interest.py`, `engine/explain.py`, `tests/test_interest.py`

**Key points:**
- New charges enter the balance on their **posting date**.
- **TRANSACTOR** → the grace period holds. Unbilled charges accrue nothing, `interest_avoided` is
  **$0**, and `NO_INTEREST_TO_AVOID` is emitted **instead of** a claim. Not a small number — zero.
- **REVOLVER** → no grace. Charges accrue daily from posting, exactly as the existing model treats
  principal.
- If charges outrun payments, `_check_amortizing()` raises, `claimable_interest_avoided()` returns
  `None`, and **no claim is made.** This is already the correct behavior — it has simply never been
  fed the charges that would trigger it. For a household whose card grows faster than they pay it,
  the honest output is not a smaller number. It is nothing, plus a sentence on the dashboard saying
  the sweep is not their problem (see `0016`).
- **The counterfactual remains `observed_monthly_payment`, never `minimum_payment`.** A test already
  pins that changing the minimum cannot move the claim by a cent (`docs/implementation-notes.md`,
  2026-07-13). This work must not weaken it — re-assert it against `Card`.

**Test scenarios:** see the plan's U5 section — a revolver charging $1,500 against $1,200 of payments
has a growing balance and produces **no figure at all**; a transactor's `interest_avoided` is exactly
$0; a mid-cycle charge accrues from its posting date for a revolver and not at all for a transactor;
changing `minimum_payment` still moves the claim by exactly zero; a normally-amortizing card produces
the same figure as before this change.

**Reason-code gate:** `NO_INTEREST_TO_AVOID` introduces new dollar-figure phrasing. Add it to
`SAMPLES` in `tests/test_explain.py` in the same commit and confirm both guard tests pass — this
category of bug has shipped three times. See `0013`.

**Verification:** No interest claim is ever produced for a growing balance or for a transactor.

Full detail: `docs/plans/2026-07-14-001-feat-card-spend-portfolio-reserve-plan.md` § U5.

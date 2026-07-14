---
id: "0010"
title: Card, cycle, portfolio, and spend-profile types
type: feature
status: done
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-14-001-feat-card-spend-portfolio-reserve-plan.md
depends_on: []
created: 2026-07-14
---

# Card, cycle, portfolio, and spend-profile types

Implements **U1** of the plan. Single owner: `backend-python-agent`.

**Goal:** Land every new value type with validation, changing no behavior. Types only — nothing
reads them yet.

**Depends on:** none.

**Files:** `engine/models.py`, `tests/test_models.py`

**Key points:**
- New types: `SpendChannel`, `Recurrence`, `SpendCategory`, `CardTransaction`, `StatementCycle`,
  `PaymentBehavior`, `Card`, `CoverageState`, `UnmatchedPayment`, `CardPortfolio`, `SpendProfile`,
  `RecurringCommitment`, `CategoryStats`.
- Frozen dataclasses with `__post_init__` validation, matching the existing convention exactly:
  every `raise ValueError` embeds the offending value, and the docstring names *which direction* of
  bad data is being refused and why that direction is the dangerous one. Enums are `str, Enum`.
- **Signs follow the engine's one rule: negative is money out.** A charge is negative even though it
  *increases* what you owe. Flipping the sign because the balance is stored as a positive liability
  is exactly the bug `CashEvent.__post_init__` exists to catch.
- `Card` carries **both** a closed statement (`statement_balance`, `statement_due_date`,
  `minimum_payment` — a known fact) **and** an unbilled balance since the last close
  (`unbilled_balance`, `next_close_date` — not yet due). **Both are load-bearing:** the reserve in
  U4 needs both, and a `Card` that carries only the closed statement is what opened the safety hole
  the plan's review caught.
- `SpendCategory` adopts Plaid's Personal Finance Category taxonomy rather than inventing one — it is
  what the real ingestion layer will hand us, and a bespoke taxonomy would need a lossy mapping on
  day one.
- `grace_days < 21` is rejected: Reg Z requires ≥21 on cards that charge interest, and a shorter
  grace places the due date earlier than the law allows — which under-reserves.
- Reuse `engine/interest.py::_statement_day()` for month-length clamping. Do not write a second one.

**Test scenarios:** see the plan's U1 section — sign convention, negative balances, APR range
(`None` means unknown, not zero), the ≥21-day grace floor, Feb clamping of a 31st close day,
`interest_bearing_balance` differing by behavior, a `CardPortfolio` that claims `COMPLETE` coverage
while carrying unmatched payments.

**Verification:** New types importable and validated. No existing test changes behavior. Full suite
green.

Full detail: `docs/plans/2026-07-14-001-feat-card-spend-portfolio-reserve-plan.md` § U1.

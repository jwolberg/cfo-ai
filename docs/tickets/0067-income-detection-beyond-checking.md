---
id: "0067"
title: A linked household's income is read from checking only — real payroll in another account is invisible, and refunds are misread as salary
type: fix
status: open
priority: medium
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
created: 2026-07-23
---

# Detect income wherever it lands, not just in checking

Spawned from operating the console against Neon (2026-07-23). Because the linked adapter reads only
checking movements and classifies **any inflow as payroll**, `hh_demo_plaid`'s detected income is
wrong in both directions at once: the real paycheck is invisible, and three unrelated refunds are
promoted to "salary".

## The defect

Two compounding rules:

1. `build_linked_history` reads only the checking account (`backend/linkedpath.py:88–96`), same root
   as `0065`.
2. `_txn` classifies every checking **inflow** as `PAYROLL` (`linkedpath.py`: `if movement.amount >
   ZERO: kind = PAYROLL`), and `_payroll` then takes the highest-confidence recurring inflow stream
   as the household's salary.

Measured on `hh_demo_plaid`:

- The real paycheck — `ACH Electronic Credit GUSTO PAY`, **$5,850/month** — lands in the **Money
  Market** account, which the adapter never reads. It is completely invisible to the engine.
- The only checking inflows are three **United Airlines** refunds of $500. So the derived
  `PayrollSpec` is `net_pay = $500.00, cadence = MONTHLY`, and the snapshot carries four
  `CashEvent(label='payroll', amount=500.00)` built from airline refunds.
- With $500/mo of phantom income and the real $5,850 unseen, the derived `opening_balance` is
  **−$1,037.62** — the engine believes a solvent household is underwater.

For a decision engine whose entire forecast pivots on projected cash, misidentifying income by an
order of magnitude and inverting the sign of the balance is not a cosmetic error.

## The fix

Two independent corrections; do both:

- **Look beyond checking for income.** Consider recurring inflows across the household's depository
  accounts (money market, savings, cash management), not just the funding account, when detecting
  payroll. The funding account stays the checking account for the forecast; income detection is a
  separate question about where salary actually arrives.
- **Don't treat every inflow as payroll.** A refund, a transfer between the household's own
  accounts, or an interest credit is not salary. Use the recurring-stream signal
  (`backend/recurring.py`) and cadence/amount/name evidence to distinguish a paycheck from a
  one-off or a self-transfer, rather than the current "amount > 0 ⇒ payroll" shortcut. An inflow
  that is not confidently income should not anchor a `PayrollSpec`.

Failing to `NoLinkedHistory` when **no** income can be confidently detected is still the safe
direction — better than inventing a $500 salary from refunds.

## Acceptance criteria

- [ ] Income detection considers recurring inflows across the household's depository accounts, not
      the funding account alone.
- [ ] On the Sandbox `user_good` dataset, the detected payroll is the GUSTO paycheck
      (~$5,850/month), not the $500 airline refunds; `opening_balance` is no longer negative for a
      solvent household.
- [ ] A non-recurring or self-transfer inflow does not, by itself, produce a `PayrollSpec` — a test
      pins that refunds/transfers are excluded from income.
- [ ] Seeded-archetype income detection is unchanged (they have a single clean payroll stream) — the
      engine/seed suites stay green.

## Notes

- Shares the checking-only root cause with **`0065`**; they can be fixed together (one "read more
  than checking" change) or separately (attribution vs income are different consumers of the wider
  read). Filed apart because the *decisions* they unblock differ: `0065` unblocks any decision at
  all; this one makes the forecast's income honest.
- The `SpendSpec` on a linked household is a deliberate placeholder (`_PLACEHOLDER_SPEND`) and is
  **not** in scope here — the decision path does not read it (`linkedpath.py` docstring).

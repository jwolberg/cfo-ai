---
id: "0036"
title: plaid_transactions, append-only
type: feat
status: done
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-17-001-feat-plaid-transport-rung-plan.md
depends_on: ["0034"]
created: 2026-07-17
completed: 2026-07-17
---

# plaid_transactions, append-only

The table `/transactions/sync` lands rows in (U3). One row per sync outcome — added, modified,
removed — and every one of them an INSERT.

## What it is

`plaid_transactions` in `backend/db/models.py`, added to `HOUSEHOLD_SCOPED` (count now eight), and
created by `alembic/versions/0006_plaid_transactions.py` with the same RLS shape as
`0001`/`0005`. The columns follow the plan's schema: `household_id` FK, `plaid_item_id`,
`plaid_account_id`, `plaid_transaction_id`, `pending_transaction_id` (a column with no consumer —
its reconciliation is normalization, deferred), `amount`/`date`/`name`/`merchant_name` (all
**nullable**), `change_type`, `ingested_at`.

## Two design decisions the schema enforces

1. **Append-only is a grant, not a convention.** `cfo_app` is granted **SELECT and INSERT and
   nothing else** — the one table whose grant differs from every other scoped table's
   SELECT/INSERT/UPDATE/DELETE. No application path can rewrite or erase a transaction even by
   mistake; a correction can only ever be a new row. Verified by trying an UPDATE and a DELETE as the
   app role and being refused at the privilege layer (`permission denied`), with the original row
   unchanged afterward.
2. **No UNIQUE on `plaid_transaction_id`.** A legitimate second `modified` of the same transaction
   shares it, and so does a later `removed`. A UNIQUE would reject exactly the corrections
   append-only exists to keep. Verified by landing the same `plaid_transaction_id` three times —
   added, modified, removed — as three distinct rows.

## The removed shape is sparse, and NULL is honest

A Plaid `removed` event carries only `transaction_id` and `account_id`. So the value columns are
nullable and a removed row lands with `amount`/`date`/`name`/`merchant_name` all NULL — the honest
absence, not a fabricated full row. Verified directly.

`amount` is `NUMERIC(14,2)`, never float (ADR-0002 [2.2]); a `Decimal` round-trips exactly.

## Verified against a real Postgres

- Migration up/down/up clean on a fresh DB from `0001`; `plaid_transactions` RLS enabled AND forced;
  `cfo_app` grant is exactly `{SELECT, INSERT}`.
- `plaid_transactions` rides the `HOUSEHOLD_SCOPED` parametrization in `test_schema.py` and
  `test_idor.py`; the IDOR `two_households` fixture now inserts a transaction per household, so the
  leak test exercises it with real rows rather than an empty table.
- Full suite green: 548 passed, 1 skipped, on Postgres 17.

## Acceptance criteria

- [x] `plaid_transactions` created by a migration; RLS enabled AND forced; added to `HOUSEHOLD_SCOPED`
      (migration names the table literally, 0033's rule).
- [x] Append-only enforced at the privilege layer (SELECT/INSERT grant only); proven by refused
      UPDATE/DELETE as the app role.
- [x] `change_type` CHECK on added|modified|removed; a fourth value rejected.
- [x] A Plaid-shaped `removed` (ids only) inserts with the value columns NULL.
- [x] `amount` is NUMERIC and round-trips a `Decimal` exactly.
- [x] Migration up and down clean on a fresh database.

## Follow-up worth considering

- `spend_projections` still carries `backend/db/models.py:215`'s "the one table here that ingest
  deletes. Ticket 0031." annotation. Per the plan, `plaid_transactions` does **not** die in this rung
  (the `assemble_snapshot()` seam is not crossed); its deletion belongs to the serving rung. Left the
  annotation on `spend_projections` untouched.
- `pending_transaction_id` is stored and never read here. Its consumer is normalization's
  reconciliation (deferred, out of this rung).

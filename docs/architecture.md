# Architecture

> Evergreen system overview — **edit in place** (unlike append-only ADRs). Keep it
> matching the shipped code; `/session-end`'s consistency check and
> `/document-audit` flag drift. Headings carry `[N]` / `[N.M]` anchors so any part
> is greppable (`grep -n "\[2\]" docs/architecture.md`) and referenceable as
> `ARCH#2`.

anchor: ARCH

**Status:** proposed — Phase 1, no code shipped yet (draft as pasted 2026-07-12,
unedited). Every section below is intent, not reality, until the corresponding
component lands.

## [1] Overview

AI Debt Autopilot answers one question daily: *how much can I safely pay toward
credit card debt today without creating financial risk?* It ingests a user's
checking, savings, and credit card data via Plaid, forecasts near-term cash flow,
and produces a conservative "safe extra payment" recommendation with a
human-readable explanation. Phase 1 does not move money — the user pays manually.

### [1.1] Guiding principles

- Deterministic financial calculations
- AI only for explanation
- Event-driven architecture
- Provider abstraction
- Auditability
- Conservative risk management

## [2] Components

### [2.1] Technology stack

**Frontend:** React, Next.js, TypeScript, Tailwind CSS, shadcn/ui, Recharts.

**Backend:** Python, FastAPI, Pydantic.

**Database:** PostgreSQL.

**Background workflows:** Temporal (preferred); Redis (cache); Cloud Tasks or
managed queues if required.

**Infrastructure:** Google Cloud Run, Cloud SQL, Secret Manager, Cloud Storage,
GitHub Actions.

**Monitoring:** Sentry, OpenTelemetry, structured logging.

**Authentication:** Clerk (or Auth0).

**Financial connectivity:** Phase 1 — Plaid (transactions, balance, liabilities).
Future — MX, Finicity.

**AI:** OpenAI or Anthropic. Used **only** for explanations, natural-language
queries, and scenario analysis. **Never** for financial calculations, optimization,
or payment decisions.

### [2.2] Financial provider layer

Abstracts provider APIs so future providers are interchangeable.

```
FinancialProvider
  connect()
  sync_accounts()
  sync_transactions()
  sync_liabilities()
```

### [2.3] Transaction pipeline

Webhook ingestion, deduplication, pending → posted reconciliation, categorization,
internal transfer detection.

### [2.4] Financial ledger

Stores accounts, transactions, balances, debt, forecasts, recommendations, and
historical outcomes. Every financial event is append-only.

### [2.5] Recurring event engine

Detects payroll, rent, mortgage, utilities, subscriptions, insurance, and credit
card minimums. Stores expected date, expected amount, and confidence for each.

### [2.6] Cash forecast engine

**Inputs:** current balance, future income, known obligations, predicted
discretionary spending, user safety buffer.

**Outputs:** daily projected balances, confidence intervals, lowest projected
balance.

### [2.7] Optimization engine

Deterministic Python, no LLM. Algorithm:

1. Protect minimum payments.
2. Maintain safety buffer.
3. Forecast 30 days.
4. Allocate remaining cash.
5. Maximize interest reduction.

### [2.8] Recommendation engine

Produces safe payment amount, confidence, interest savings, updated payoff date, and
a human-readable explanation.

### [2.9] API modules

`/auth`, `/accounts`, `/transactions`, `/debts`, `/forecast`, `/recommendations`,
`/settings`, `/notifications`.

## [3] Data flow

High-level layering:

```
User → Next.js client → FastAPI API → Financial services layer
  (Plaid integration · transaction sync · balance sync · liability sync)
→ Normalization layer → Financial ledger → Recurring transaction detection
→ Cash flow forecast engine → Debt optimization engine → Recommendation engine
→ Notification service → User dashboard
```

Event flow on new data:

1. Plaid webhook arrives.
2. Transaction sync.
3. Normalize.
4. Store.
5. Detect recurring activity.
6. Update forecast.
7. Recompute recommendation.
8. Notify customer if the recommendation changed.

## [4] Key decisions

<Pointers to load-bearing ADRs once written. This section is the index into
docs/decisions/.>

## [5] External dependencies & services

Plaid (bank data), Clerk or Auth0 (auth), OpenAI or Anthropic (explanations only),
Google Cloud (Cloud Run, Cloud SQL, Secret Manager, Cloud Storage), Temporal
(workflows), Sentry + OpenTelemetry (observability).

## [6] Conventions

### [6.1] Engineering philosophy

Every financial recommendation must be deterministic, reproducible, explainable,
auditable, and conservative.

The system's competitive advantage is not its user interface or LLM integration. It
is the quality of its forecasting engine and the trust it earns through consistently
correct financial recommendations.

## [7] Future extensions

| Phase | Extension |
| --- | --- |
| 2 | Payment orchestration |
| 3 | ACH engine |
| 4 | Emergency credit |
| 5 | Treasury optimization |
| 6 | AI-native banking platform |

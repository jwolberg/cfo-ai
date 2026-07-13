# Product Requirements Document — Phase 1: AI Debt Autopilot

**Status:** draft (as pasted 2026-07-12, unedited)

## Executive summary

### Vision

Build the world's first AI-native banking platform: an autonomous financial system
that continuously optimizes a customer's financial life rather than simply reporting
on it.

Traditional financial software tells users what happened. AI banking decides what
should happen next.

The long-term platform will optimize:

- Cash management
- Debt repayment
- Savings
- Investing
- Taxes
- Credit
- Insurance
- Financial planning

However, this vision is too broad for an initial product.

### Phase 1 goal

Build the most trusted AI system for safely accelerating credit card debt repayment.

The system answers one question every day:

> "How much can I safely pay toward debt today without creating financial risk?"

This creates measurable customer value while establishing the trust required for
broader autonomous financial management.

## Problem statement

Millions of Americans carry revolving credit card balances despite maintaining
positive cash balances throughout the month. This happens because customers optimize
for liquidity rather than interest expense.

Typical behavior:

- Leave several thousand dollars idle in checking
- Continue paying 20–30% APR
- Wait until statement dates
- Fear overdrafts and unexpected expenses

The result is years of unnecessary interest payments. Today's budgeting applications
explain this problem. None solve it automatically.

## Why this is the correct entry point

Launching an "AI Bank" immediately requires users to trust software with every
financial decision. That trust must be earned.

Credit card optimization provides:

- One clear customer problem
- Quantifiable savings
- Daily engagement
- Objective success metrics
- A foundation for future financial automation

Customers begin by trusting recommendations. Later they trust automation. Eventually
they trust autonomous banking.

## Product principles

- Deterministic financial decisions
- Conservative recommendations
- Explain every recommendation
- Never surprise users
- Build trust before automation
- AI explains decisions — it does not calculate money movement

## Target customer

**Primary customer**

- Stable employment
- Positive monthly cash flow
- Revolving credit card debt
- Wants debt eliminated
- Not interested in detailed budgeting

**Typical profile**

- $8,000–40,000 credit card debt
- Income sufficient to repay
- Leaves cash idle for safety
- Pays more than minimum payments
- Feels financially responsible but inefficient

## User stories

As a user...

- I want to connect my checking account and credit cards.
- I want to know how much money is truly available today.
- I want confidence that paying extra will not cause an overdraft.
- I want to understand why the recommendation changed.
- I want to see my debt-free date improve over time.

## MVP features

### Account connection

Connect checking, savings, and credit cards via Plaid.

### Daily recommendation engine

Every morning calculate the **safe extra payment**. Display:

- Recommended payment
- Confidence
- Interest saved
- New projected payoff date

### Cash flow forecast

Forecast income, bills, typical discretionary spending, and pending transactions.
Produce a conservative recommendation.

### Debt dashboard

Show current balances, APR, interest paid, projected payoff date, savings created,
and historical recommendations.

### Recommendation explanation

Example:

> "We forecast your paycheck arriving Friday. Your rent clears Tuesday. Maintaining
> your selected $750 safety buffer leaves $126 available today."

### Manual payment

Phase 1 intentionally does **not** move money. The user makes the payment themselves.
This dramatically lowers operational complexity while validating recommendation
quality.

## Out of scope

- Automatic payments
- Lending
- Banking accounts
- Budgeting
- Investments
- Tax optimization
- Insurance
- Subscription cancellation
- Chat-based financial planning

## Success metrics

**Primary KPI:** average reduction in projected debt payoff duration.

**Secondary metrics**

- Average monthly interest saved
- Recommendation acceptance rate
- Daily active users
- Monthly retention
- Forecast accuracy
- User trust score
- Connected account retention

## Future phases

| Phase | Scope |
| --- | --- |
| 2 | User-approved payments |
| 3 | Rule-based automatic payments |
| 4 | Emergency liquidity |
| 5 | AI treasury |
| 6 | AI banking platform |

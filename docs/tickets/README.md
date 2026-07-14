# Backlog tickets

Local ticket set for `docs/plans/2026-07-13-001-feat-decision-engine-frontend-mvp-plan.md`,
one per Implementation Unit (U8 split into two — see `0008`/`0009` — since it bundles two
different dependency chains). Written here rather than filed through the factory
(`mcp__terminal-harness__file_ticket`) because this repo isn't yet a registered repo in
that system — only `project-template` is. The frontmatter schema mirrors the factory's
own ticket format (`agentId`/`agentKind`/`agentScope`, `depends_on`) so these can be
re-filed there directly once `cfo-ai` is registered.

Every ticket has exactly one owner. Owners are placeholder role identities
(`backend-python-agent`, `backend-llm-agent`, `mobile-rn-agent`, `infra-devops-agent`) —
substitute real agent IDs once this repo has one registered (`list_agents` would list
them).

## Dependency order

```
0001 (precompute)
  └─ 0002 (FastAPI scaffold)
       ├─ 0003 (base narration) ──────────┐
       ├─ 0004 (LLM assistant) ───────────┤
       ├─ 0005 (Expo scaffold)            │
       │    └─ 0006 (dashboard) ◄─────────┘
       │         └─ 0007 (assistant modal) ◄── 0004
       ├─ 0008 (CI: backend + mobile) ◄──── 0005
       └─ 0009 (Cloud Run deploy) ◄──────── 0003, 0004, 0008 (recommended, not blocking)
```

| Ticket | Title | Owner | Depends on |
|---|---|---|---|
| [0001](0001-household-decision-history-precompute.md) | Household decision-history precompute | backend-python-agent | — |
| [0002](0002-fastapi-service-scaffold.md) | FastAPI service scaffold | backend-python-agent | 0001 |
| [0003](0003-base-narration-endpoint.md) | Base narration endpoint | backend-python-agent | 0001, 0002 |
| [0004](0004-explain-assistant-llm-endpoint.md) | Explain-assistant LLM endpoint | backend-llm-agent | 0001, 0002 |
| [0005](0005-expo-app-scaffold.md) | Expo app scaffold | mobile-rn-agent | 0002 |
| [0006](0006-dashboard-screen.md) | Dashboard screen | mobile-rn-agent | 0003, 0005 |
| [0007](0007-explain-assistant-modal.md) | Explain assistant modal | mobile-rn-agent | 0003, 0004, 0006 |
| [0008](0008-ci-backend-and-mobile.md) | CI: lint/test backend + typecheck mobile | infra-devops-agent | 0002, 0005 |
| [0009](0009-cloud-run-deployment.md) | Cloud Run deployment | infra-devops-agent | 0002, 0003, 0004, 0008 (recommended) |

---

# Card spend and the portfolio reserve

Ticket set for `docs/plans/2026-07-14-001-feat-card-spend-portfolio-reserve-plan.md`, one per
Implementation Unit (U1–U8 → `0010`–`0017`).

The feature makes credit-card spend a first-class engine input and, in doing so, closes a hole in
the safety core: the reserve currently holds back each card's **minimum payment** when it should hold
back each card's **obligation**. A household that charges $2,000/month to a second card and pays it
in full has a $2,000 obligation and a $40 minimum. We reserve $40.

## Dependency order

```
0010 (types)
  ├─ 0011 (simulator: card charges, 2nd card) ──┐
  ├─ 0012 (derivation: cards, spend, coverage) ─┤
  │    └─ 0015 (GET /spend)                     │
  │         └─ 0016 (mobile: tabs + Spending)   │
  └───────────────────────────────────────► 0013 (engine: obligation reserve) ◄─┘
                                                 └─ 0014 (interest: charges, grace)

0017 (grader + replay driver) — detached on purpose; see below
```

| Ticket | Title | Owner | Depends on |
|---|---|---|---|
| [0010](0010-card-spend-types.md) | Card, cycle, portfolio, and spend-profile types | backend-python-agent | — |
| [0011](0011-simulator-card-charges.md) | Simulator — actually charge the card | backend-python-agent | 0010 |
| [0012](0012-derivation-cards-spend-profile-coverage.md) | Derivation — cards, spend profile, coverage detector | backend-python-agent | 0010 |
| [0013](0013-engine-obligation-reserve.md) | Engine — the obligation reserve, the reason codes, the forecast | backend-python-agent | 0011, 0012 |
| [0014](0014-interest-cards-get-charged.md) | Interest — cards get charged | backend-python-agent | 0010, 0013 |
| [0015](0015-backend-spend-endpoint.md) | Backend — `GET /spend` | backend-python-agent | 0012 |
| [0016](0016-mobile-spending-surface.md) | Mobile — bottom tabs, Spending screen, attestation gate | mobile-rn-agent | 0015 |
| [0017](0017-grader-and-replay-driver.md) | Wire the grader and build the replay driver | backend-python-agent | — |

## Three things to know before picking one of these up

**`0013` is the whole feature.** It carries the safety fix, and the first draft of its design opened
the very hole it exists to close — reserving only the *closed* statement leaves the reserve at **$0**
for roughly the last third of every cycle, because today's reserve is a rolling forecast and
`Card.statement_due_date` is a fact about a statement already paid. The reserve must cover **two**
statements: the closed one, and the one that will close and come due inside the horizon. Read the
warning block in `0013` before writing any of it.

**`0011` is an import-time crash if you scope it naively.** The `HouseholdSpec.card` → `cards` rename
breaks `DEMO_SPEC`'s construction at module load, so `backend/precompute.py`, `tests/test_precompute.py`
and `tests/test_outcome.py` all move in the same commit. `card_share=0` does not save you.

**`0017` is detached deliberately.** It builds the measurement harness that would *license* loosening
the forecast's spend model. Nothing in `0010`–`0016` waits on it, and until it lands,
`daily_discretionary_high` stays exactly as it is. The reserve work **tightens** (always safe); the
spend-model swap would **loosen** (needs the measurement). That distinction is the rule, not a
compromise.

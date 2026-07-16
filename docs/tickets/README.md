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

## Status: all eight landed (2026-07-14)

`0010`–`0013` shipped in **#28**, `0014`–`0017` in **#29**. 357 Python tests, 39 mobile tests.

Two exceptions worth carrying forward rather than closing quietly:

- **`0016` did not build the attestation action.** `UNATTESTED` is a blocking refusal and its copy
  reaches the feed, but *attesting* is a write and this backend has no database by design (ADR
  `0002`). So `CARD_COVERAGE_INCOMPLETE` is the one refusal in this feature nobody has seen
  end-to-end in the product.
- **`0017` produced a number, not a licence.** Breach rate **6.9%**, zero sweep-caused overdrafts,
  on one household and one seed. `daily_discretionary_high` still has not moved and must not until
  that is a distribution.

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

---

# Multi-tenant persistence, and the households this engine has never seen

Ticket set for `docs/plans/2026-07-16-001-feat-multi-tenant-persistence-plan.md`, one per
Implementation Unit (U1–U8 → `0019`–`0026`). Decision:
[ADR-0004](../decisions/0004-postgres-scoped-by-household.md), superseding `0002`.

The ask was "a database, seeded with a couple of customers, so I can understand the dashboards."
The hard part is neither: `sim/` is already a correct multi-household primitive, `build()` already
takes `spec`/`seed`/`policy`, and single-tenancy lives in four places. Scoping it turned up two
defects instead — see below.

## Dependency order

```
0019 (unify the walk) — detached on purpose; ships first, needs no database
        │
0020 (schema, RLS, migration)
  ├─ 0021 (repository + IDOR suite) ──┐
  ├─ 0022 (SnapshotStore seam) ───────┤
  └─ 0026 (Neon + deploy path)        │
                                      ├──► 0023 (archetypes + seeder) ◄── 0019
                                      │        └─ 0024 (read path)
                                      │             └─ 0025 (mobile switcher)
                                      └────────────►┘
```

| Ticket | Title | Owner | Depends on |
|---|---|---|---|
| [0019](0019-unify-the-walk.md) | The walk, unified — and the dial nothing reads | backend-python-agent | — |
| [0020](0020-schema-rls-first-migration.md) | Schema, RLS, and the first migration | backend-python-agent | — |
| [0021](0021-repository-scoping-and-idor-suite.md) | Repository scoping and the IDOR suite | backend-python-agent | 0020 |
| [0022](0022-snapshot-store-seam.md) | The `SnapshotStore` seam | backend-python-agent | 0020 |
| [0023](0023-archetypes-and-seeder.md) | The archetypes, and the seeder | backend-python-agent | 0019, 0021, 0022 |
| [0024](0024-read-path-tenancy.md) | The read path — serve from Postgres, scoped by household | backend-python-agent | 0021, 0023 |
| [0025](0025-mobile-household-switcher.md) | Mobile — the household switcher | mobile-rn-agent | 0024 |
| [0026](0026-neon-and-deploy-path.md) | Neon, and the deploy path | infra-devops-agent | 0020 |

## Three things to know before picking one of these up

**`0019` is a defect, not a refactor, and it ships alone.** `assemble_snapshot()`'s docstring says it
exists so `build()` and `replay()` do not drift. They have drifted: `build()` does not call it,
constructs a `Snapshot` inline at `precompute.py:943-973` that never sets `spend_30d_high`, and has
no `spend_quantile` parameter at all — while `calibrate.py` sweeps nine settings of that dial through
`replay()`. Invisible today only because `SPEND_QUANTILE = None`. **The day the dial moves,
`calibrate.py` licenses a forecast the artifact cannot ship** — the harness grading an engine that is
not the one serving. `decision-engine.md` §8.4's bug class, and §6.6's test guards the *measurement*,
not the *wiring*.

Its verification **is** the ticket: `tests/test_precompute.py:489-494` stays green **without being
regenerated**, plus a new test that fails on today's code. Regenerating the committed artifact to
match new output deletes the only evidence the refactor preserved behavior.

**`0023` is what the plan exists for.** Every household this engine has ever run against is
biweekly, one card, 23.99% — including all 60 in the calibration population, because
`calibrate._spec_for()` varies only the spend shape. So `decision-engine.md` §9.3's admission that
the 7-day spacing rule is "a poor approximation for everyone else" has never been tested: there is no
everyone else. If archetypes B/C/D refuse constantly, **that is the finding**, not a bug — and not a
reason to loosen a gate.

**`0021` tests a mechanism whose identity is fake.** The IDOR suite is real and must be; the auth
feeding it is a shared API key that may select any household (`0024`). That gap is deliberate —
synthetic households have no owner to authenticate as, and Clerk lands with Plaid — but it must be
flagged in `USERS.md`, not only in a code comment. An IDOR suite is reassuring in a way a shared key
does not earn.

## The number that was wrong

`architecture.md` [4.1] first justified `0022`'s seam with a **guessed** ~10KB snapshot and 18 TB/yr,
and reasoned a whole "phase 3" out of it. Measured: **2,699 B**, compressing **70×** when sorted by
`(household_id, day)` — under a TB/yr at 5M households. The seam survives on a *compression gap*
(Postgres TOAST does ~3.5×), not a volume wall, and phase 3's trigger cannot fire. `prd.md` §2.4
documents two prior plugged-in numbers that "pointed the right way for the wrong reason"; this was
nearly the third, and `0022` is scoped to the smaller claim.

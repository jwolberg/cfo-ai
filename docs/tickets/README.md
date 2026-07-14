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

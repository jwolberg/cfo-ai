---
name: factory
description: "Continuous autonomous orchestrator: reconcile, route tickets by owner agent, run stacked PR/MR passes, optionally discover/refill, park HITL, never merge. Use on /factory or 'work the backlog autonomously'."
---

# /factory — Continuous Orchestrator

`/factory` is the loop around `/stacked-mr`; it does not invent a separate
build/review bar. It keeps moving while runnable work remains, parks true human
blockers in HITL, and leaves merging to the human.

The canonical owner, knowledge, delegated-artifact, and follow-up contract is
[`docs/workflow/agent-process.md`](../../../docs/workflow/agent-process.md).
Factory treats classic agents, repo/global scripts, and persistent agents as
one assignable inventory. Ticket ownership is always the
`agent_id` + `agent_scope` + `agent_kind` tuple; model policy, quality gates,
and output judges live on the selected agent definition and should not be
reimplemented in this skill.

## MCP Fast Path

Use MCP for deterministic orchestration state when available:

- `list_tickets({repo, status})` / `get_ticket({slug})`
- `list_agents({repo})`
- `update_ticket_agent({slug, agentId, agentScope, agentKind})`
- `update_ticket({slug, status: 'in-progress'})`
- `update_ticket_run({slug, runId: $TERMINAL_RUN_ID, runSource: 'agent', runStartedAt, runStatus: 'running'})` when a run id exists
- `request_agent_artifact({repo, title, prompt, agentId, agentScope, agentKind})`
- `file_ticket({repo, title, body, type, priority, source, agentId, agentScope, agentKind})`
- `emit_activity({kind, repo, title, detail})`

Fallback command helpers: `.claude/bin/list-agents` and
`.claude/bin/request-agent-artifact`.

## Invocation

```text
/factory
/factory "payments + auth"
/factory --discover
/factory --max-stack 12
```

## Loop

```text
/merge-sync
  -> select runnable owner lane
  -> mark in-progress and link pickup run/session when available
  -> /stacked-mr pass
  -> handle stuck/HITL lanes
  -> if queue empty and --discover, run discovery/check agents
  -> repeat until no runnable scoped work remains
```

## Responsibilities

| Step | Factory responsibility |
|---|---|
| Reconcile | Run `/merge-sync`; clear stale `stuck` tickets when blockers are gone. |
| Route | Group work by `agent_id` / `agent_scope` / `agent_kind` across classic and persistent agents; split multi-agent work into linked tickets. |
| Observe | At pickup, set `in-progress` and write `agent_run_*` fields with `update_ticket_run` when a run id is available. |
| Build/review | Call `/stacked-mr`; do not duplicate its implementation or review logic. The review checkpoint is the `code-review` agent, with `/code-review` only as a compatibility launcher. `/stacked-mr` also yields one joint `/digest` over the batch (its Phase 2.5) — the human's read surface; do not gate on it. |
| Discovery | When `--discover` is enabled, run discovery/check agents and file owner-scoped tickets. |
| HITL | File true human blockers, then continue independent lanes. |
| Context | Compact at pass boundaries; state of record lives on disk. |

## Delegation

Factory stays thin. Use these patterns:

| Pattern | Use for |
|---|---|
| `request_agent_artifact` | Focused cross-domain knowledge questions with durable reports. |
| `list_agents` | Resolve assignable classic and persistent owner agents. |
| `code-review` agent | Review/test artifacts. `/code-review` remains a compatibility launcher. |
| `/stacked-mr` | Full build stack. |
| Fresh worktree/session | Long-running owner lane that needs isolation. |

Keep only summaries and artifact paths in factory context.

## Discovery

Discovery is off unless `--discover` is passed. When enabled:

1. Run the relevant discovery/check agents.
2. Use `list_agents({repo})` or `.claude/bin/list-agents`.
3. File every finding as a ticket with exactly one owner and a concrete quality/eval expectation.
4. Split multi-phase or multi-owner findings into linked tickets with `depends_on`.
5. Loop back to `/merge-sync`.

## Final State

Report state, not a handoff request:

- passes run
- PR/MR stack in merge order
- tickets at the bar
- stuck/HITL lanes
- follow-up/discovery tickets filed

## Hard Rules

1. Never merge.
2. Never alter `/stacked-mr`'s review bar.
3. Never create ownerless tickets.
4. True human-only decisions go to HITL; review failures iterate.
5. No "tell me when ready" while runnable scoped work remains.
6. Do not hardcode persona names or `/code-review`; route through agent definitions.

---
name: ticket
description: "Create and manage in-repo backlog tickets (.TerMinal/backlog/NNNN-slug.md in v2, backlog/NNNN-slug.md in legacy v1); atomic ids, list/update/close. Portable. Use on /ticket, 'file/list/close a ticket', or describing work to track."
---

# /ticket — In-repo backlog tickets

In-repo markdown tickets at `.TerMinal/backlog/NNNN-slug.md` in v2 repos
(legacy v1: `backlog/NNNN-slug.md`) — versioned with the code, no external
service.

## Fast path: TerMinal MCP tools (skip the rest if available)

When the `terminal-harness` MCP server is registered (ships with TerMinal,
installs via Settings), use these instead of reading sections below:

- **`list_agents({repo?})`** — compact assignable agent list
  (`id`, `scope`, `kind`) covering global defaults, repo-local agents, and
  persistent agents.
- **`file_ticket({repo, title, body?, type?, priority?, status?, source?, agentId?, agentScope?, agentKind?})`** —
  allocates next id, writes frontmatter, returns `{slug, id, path}`.
- **`update_ticket_agent({slug, agentId, agentScope?, agentKind?})`** —
  assigns exactly one agent to an existing ticket.
- **`update_ticket_run({slug, runId, runSource?, sessionId?, runStartedAt?, runStatus?})`** —
  links a ticket to the run/session that picked it up.
- **`update_ticket({slug, status?, priority?, appendPrUrl?, removePrUrl?, agentId?, agentScope?, agentKind?, runId?})`** —
  whitelisted mutation, auto-bumps `updated:`.
- **`list_tickets({repo?, status?, type?})`** / **`get_ticket({slug})`**.
- **`set_run_outcome({runId: $TERMINAL_RUN_ID, outcome: 'ticket-filed'})`** —
  call after filing when running as a scheduled / `/bg` agent (skip when
  interactive — no TERMINAL_RUN_ID).

Saves ~7k tokens vs reading SKILL.md + EXAMPLE.md. The thinker work
(when to file, type/priority judgment, drafting ACs) still belongs to
you. Shell-helper path below is the fallback when MCP isn't installed; use
`.claude/bin/list-agents` as the non-MCP fallback for owner-agent selection.

---

## Where tickets live

`<repo-root>/.TerMinal/backlog/NNNN-kebab-slug.md` in v2 repos. Schema:
[`EXAMPLE.md`](./EXAMPLE.md). Counter at `.TerMinal/backlog/.next-id`.
Legacy v1 repos that already have `backlog/` continue to use `backlog/.next-id`.

## Helper scripts (carried by this skill)

```bash
ROOT="$(git rev-parse --show-toplevel)"
SKILL="$ROOT/.claude/skills/ticket"
BACKLOG_DIR=$([ -d "$ROOT/backlog" ] && [ ! -f "$ROOT/.TerMinal/template.json" ] && echo "$ROOT/backlog" || echo "$ROOT/.TerMinal/backlog")

"$SKILL/bin/next-ticket-id"      # atomically allocate next id
"$SKILL/bin/tickets"             # list all
"$SKILL/bin/tickets open"        # by status
"$SKILL/bin/tickets open high"   # status + priority
"$SKILL/bin/tickets future"      # by horizon
```

`next-ticket-id` uses an `mkdir` lock (parallel-safe, no `flock`) and
bootstraps the active backlog directory if missing. Both must be executable.

## Routing

- **Create** ("file a ticket", "/ticket <desc>") → §Create
- **List** ("what's open", "future ideas") → `bin/tickets [status] [priority] [horizon]`
- **Update / close** ("close #42", "link the MR") → §Update

---

## Create a ticket

### 1. Gather the facts

Infer from context; ask once only if genuinely unclear.

- **Title** — short, action-oriented ("Add rate limit to signaling join").
- **Type** — `bug` | `feature` | `security` | `docs` | `dx` | `testing` | `ux` | `performance`.
- **Priority** — `critical` | `high` | `medium` | `low`. `medium` is a fine default; say so.
- **Horizon** — `now` | `next` | `future`. Scope/timeline, orthogonal to priority. Default `now`.
  `code-review` agent and `/session-end` follow-ups are usually `future` or `next`.
- **Source** — `manual`, `audit`, `feedback`, agent name (`code-review`), or a ref.
- **Refs** (optional) — plan unit IDs (`U10`), ADRs (`ADR-0002`), doc paths.
- **Agent** — every ticket is assigned to exactly one agent:
  `agent_id`, `agent_scope` (`repo` | `global`), and `agent_kind`
  (`classic` | `persistent`). Use `list_agents({repo})` when MCP is available
  or `.claude/bin/list-agents` otherwise.
  Default generic implementation work to `1000x-ai-engineer`; route docs,
  testing, security, performance, and DX/tooling tickets to their matching
  specialist when the type or content clearly signals that domain.
  If multiple phases need different agents, file multiple linked tickets and
  connect them with `depends_on`.

### 2. Allocate an id

```bash
id=$("$(git rev-parse --show-toplevel)/.claude/skills/ticket/bin/next-ticket-id")
```

Never hand-edit `.next-id` — use the script.

### 3. Write the file

Path: `$BACKLOG_DIR/<id>-<kebab-slug>.md` (slug ≤ 6 words).

```yaml
---
id: <int, matches filename prefix>
title: "<title>"
status: open
priority: <critical|high|medium|low>
horizon: <now|next|future>
hitl: <true|false>
type: <bug|feature|security|docs|dx|testing|ux|performance>
source: <where it came from>
created: <YYYY-MM-DD>
updated: <YYYY-MM-DD>
prs: []
refs: []
depends_on: []
agent_id: <agent id>
agent_scope: <repo|global>
agent_kind: <classic|persistent>
---
```

Body (prose goes **after** the closing `---`):

```markdown
## Description
<2–4 sentences: what's the problem/feature, why it matters.>

## Acceptance criteria
- <Concrete, testable bullet>

## Design notes
<Optional: approach, constraints, gotchas.>

## Repro
<Bugs only.>
```

### 4. If it needs a human, raise a HITL item separately

Tickets track work; HITL is for human-only blockers (decisions, approvals,
creds, OAuth/browser flows). Raise to the global inbox (CLAUDE.md [4.2]):

```bash
.claude/bin/hitl "<title>" "<exact action + any url/options>"
```

Lead with the action; include the url. The HITL helper is append-only from the
agent side: it files the Inbox item, emits activity, and pings Telegram. Do not
edit `~/.config/TerMinal/hitl.json` directly, and do not resolve your own HITL
item. If waiting on the human, query the Inbox/list status or periodically
re-check the original blocker; continue when it no longer blocks. Reserve HITL
for true human-needs — not review feedback or test fails inside a workflow
(those iterate).

If the blocker clears after a ticket was marked `stuck`, update the ticket
status immediately: use `open` when returning it to the queue, or `in-progress`
when actively resuming it. Stale `stuck` status is drift.

### 5. Confirm and stop

Show the path. Don't auto-start the work unless asked.

---

## Update / close

Edit the file directly:
- Change `status:` (`open` → `in-progress` → `closed`; or `stuck` / `icebox`).
- When unblocked, change `stuck` → `open` or `in-progress` immediately.
- Bump `updated:` to today.
- On MR/PR open: add the url to `prs:`. On merge: `status: closed`.
- Prose strictly **after** the closing `---`.

`bin/tickets [status]` to verify.

---

## Quality bar

- **Testable acceptance criteria.** "POST /join returns 429 after 100 req/min", not "looks good".
- **One ticket = one piece of work.** Two unrelated things → two tickets.
- **No speculative tickets.** "Maybe we should..." → doc/learning, not a ticket.

## Porting to a new repo

If bootstrapped from the workflow template, `bootstrap.sh` already
installed it — skip. Standalone:

1. Copy `.claude/skills/ticket/` into the target repo.
2. `chmod +x .claude/skills/ticket/bin/*`.
3. Add `.TerMinal/backlog/.next-id.lock` (or legacy `backlog/.next-id.lock`) to `.gitignore`.

First call bootstraps the backlog directory + `.next-id`. Commit the backlog
directory so the tracker travels with the code.

## Activity

```bash
.claude/bin/activity ticket-filed "Ticket filed · #<id>" "<title>" --ticket <id>
.claude/bin/hitl "<title>" "<action needed>"  # only for human-only blockers
.claude/bin/activity ticket-closed "Ticket closed · #<id>" "<title>" --ticket <id>
```

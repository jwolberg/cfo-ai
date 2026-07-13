---
id: 0
title: "Example ticket — copy this format for new tickets"
status: open
priority: medium
horizon: now
hitl: false
type: feature
source: manual
created: 2026-05-26
updated: 2026-05-26
prs: []
refs: []
depends_on: []
acceptance: []
agent_id: 1000x-ai-engineer
agent_scope: global
agent_kind: classic
agent_run_id: ""
agent_run_source: agent
agent_session_id: ""
agent_run_started_at: ""
agent_run_status: ""
---

Canonical schema reference for in-repo tickets. This file is **not** a real
ticket (the `bin/tickets` lister only matches `.TerMinal/backlog/NNNN-*.md`
or legacy `backlog/NNNN-*.md`, and a `0` id makes it inert). Real tickets live
at `.TerMinal/backlog/NNNN-kebab-slug.md`.

## Frontmatter fields

- `id` — integer, matches the numeric prefix in the filename (`0042-...`).
- `title` — short, action-oriented, one line.
- `status` — `open` | `in-progress` | `closed` | `stuck` | `icebox`
- `priority` — `critical` | `high` | `medium` | `low`
- `horizon` — `now` | `next` | `future`. The scope/timeline tag, **orthogonal
  to priority**: `now` = in scope for current work; `next` = committed, do soon;
  `future` = extension / idea out of current scope. This is where the `code-review` agent
  and `/session-end` park follow-up ideas and extensions. Lets you filter
  "do now" vs "someday" at a glance even when priorities are similar. Default
  `now`.
- `hitl` — `true` | `false` (default `false`). **Human-in-the-loop**: the ticket
  requires a manual action only the human can take (approve a merge, provision
  credentials, click through a browser/OAuth flow, sign something, make a product
  call). When a HITL ticket is filed, `/ticket` sends a Telegram ping describing
  the action needed (depends on the machine-level telegram setup — see `/notify`).
  HITL tickets should carry an `## Action needed` body section. Filter with
  `bin/tickets hitl`.
- `type` — `bug` | `feature` | `security` | `docs` | `dx` | `testing` | `ux` | `performance`
- `source` — where it came from: `manual`, `audit`, `feedback`, an agent name (e.g. `code-review`), or a ref.
- `created` / `updated` — ISO dates (`YYYY-MM-DD`).
- `prs` — array of PR URLs that implement/fix this ticket. Populated when a PR
  is opened, not at creation. (Field name kept as `prs`; GitHub PR and GitLab
  MR URLs both parse.)
- `refs` — optional array of in-repo links: plan unit IDs (`U10`), ADRs
  (`ADR-0002`), or doc paths. Ties a ticket to the design artifacts it advances.
- `acceptance` — optional **block list** of strict, checkable criteria defining
  a correct/best implementation. Written as a YAML block sequence (one `- "…"`
  per line), so criteria may contain commas:

  ```yaml
  acceptance:
    - "join endpoint returns 429 over the rate limit"
    - "limit configurable via env, default 60/min"
    - "existing auth tests still pass"
  ```

  **Required before running more than one implementation lane.** When the
  implementer launches N>1 lanes (parallel variant attempts of this ticket),
  each lane is *gated* on tests passing AND every acceptance criterion being
  met (judged per-criterion with evidence), then eligible lanes are *ranked*
  by `/code-review` overall score (lower `risk_score` breaks ties). The winning
  lane's PR/MR is surfaced for human merge. Lane count is chosen by the
  implementer at launch — it is **not** a ticket field. Default 1 lane.
- `depends_on` — optional array of **ticket IDs** (integers) this one is
  blocked by. A ticket is **blocked** when any id in this list points to a
  ticket whose `status` is not `closed`. The Tickets tab renders a red
  `blocked` badge on the list row, and the detail view shows clickable
  dependency chips colored red when the dependency is still open. `/factory`
  treats blocked tickets as out-of-scope until their dependencies close.
- `agent_id` — exactly one agent assigned to perform this ticket. Use
  `list_agents({repo})` when the TerMinal MCP tools are available; otherwise
  choose from the Agents tab or `.agents/`.
- `agent_scope` — `repo` for repo-local agents/scripts, `global` for TerMinal
  defaults, global scripts, or persistent agents.
- `agent_kind` — `classic` for normal TerMinal agents/scripts, `persistent`
  for global persistent memory agents.
- `agent_run_id` / `agent_run_source` / `agent_session_id` /
  `agent_run_started_at` / `agent_run_status` — optional pickup observability
  fields written when an implementation agent starts. Use `update_ticket_run`
  when MCP is available; TerMinal one-click process-mode implementation writes
  them automatically.

If the work naturally needs multiple agents or phases, split it into multiple
tickets and link them with `depends_on`.

## Filename convention

`.TerMinal/backlog/NNNN-kebab-case-title.md`, e.g.
`.TerMinal/backlog/0042-rate-limit-join.md`.
Allocate the next id atomically with `.claude/skills/ticket/bin/next-ticket-id`.

## Body

Freeform markdown. Suggested sections: Description, Acceptance criteria,
Design notes, Repro (bugs only). Keep prose **after** the closing `---` of the
frontmatter, never inside the delimiters.

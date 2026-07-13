---
name: new-agent
description: "Create or update a TerMinal agent from a natural-language request — writes a script-first .agents/<id>.sh + <id>.json sidecar. Use on /new-agent, 'create an agent', or wanting a reusable button/scheduled agent."
---

# /new-agent — Create a TerMinal agent

Create a repo-local TerMinal agent as a bash script plus JSON sidecar. Agents
show up in the TerMinal Agents tab and can be run manually or scheduled.

## Target files

Default target is the current repo:

- `.agents/<id>.sh` — executable script body (`chmod 755`)
- `.agents/<id>.json` — metadata sidecar

If the user explicitly asks for a global agent, write to
`~/.config/TerMinal/scripts/<id>.sh` and `<id>.json` instead.

## Workflow

1. Read enough local context before writing:
   - `CLAUDE.md` / `AGENTS.md`
   - `.agents/scripts.md` if present
   - `.agents/forge.md` if the agent may open a PR/MR
   - existing `.agents/*.sh` and `.agents/*.json` to avoid duplicate ids
2. Pick a short kebab-case `id`, a clear `title`, a one-line `description`, and
   a lucide icon name.
3. Write the bash script. Prefer deterministic checks first, cap any context
   passed to an LLM, then call an LLM only when useful.
4. Write the sidecar JSON.
5. Verify the script is executable and summarize the paths.

## Script contract

Use this shape:

```bash
#!/usr/bin/env bash
set -uo pipefail

repo="${TERMINAL_REPO:-$(git rev-parse --show-toplevel)}"
worktree="${TERMINAL_WORKTREE:-$repo}"
engine="${TERMINAL_ENGINE:-claude}"
# Recurring/report agents should default cheap; implementation agents may leave
# this empty so the selected engine uses its normal coding default.
model="${TERMINAL_MODEL:-haiku}"

# Do deterministic work first. For TerMinal helpers:
# terminal-cli ticket "<title>" "<body>"
# terminal-cli hitl "<title>" "<action>"
# terminal-cli activity <kind> "<title>" "<detail>"
# terminal-cli state get|set <key> [value]
# terminal-cli mcp list_agents repo="$(basename "$repo")"
# terminal-cli mcp request_agent_artifact repo="$(basename "$repo")" agentId=knowledge-base title="Question" prompt="..."
#
# Portable repo-local fallbacks available in project-template repos:
# .claude/bin/list-agents
# .claude/bin/request-agent-artifact --agent knowledge-base --title "Question" -- "Prompt..."
```

When calling an engine from inside the script:

```bash
case "$engine" in
  codex) model="${TERMINAL_MODEL:-gpt-5-mini}" ;;
  cursor) model="${TERMINAL_MODEL:-composer-2.5-fast}" ;;
  *) model="${TERMINAL_MODEL:-haiku}" ;;
esac

claude -p "$prompt" --permission-mode auto ${model:+--model "$model"}
codex exec -s danger-full-access -C "$worktree" ${model:+--model "$model"} "$prompt"
cursor-agent -p --force --trust --workspace "$worktree" ${model:+--model "$model"} "$prompt"
```

Rules:

- Cap logs and file lists before interpolation (`tail -200`, `head -100`,
  `rg -n -C2 ... | head -160`). Prefer letting the engine open a specific path
  over pasting whole files into the prompt.
- Never merge to `main`/`master`.
- Open a PR/MR only when the script creates concrete code changes.
- Use `terminal-cli ticket` for findings the agent should not fix in the same run.
- Use `terminal-cli mcp list_agents` or `.claude/bin/list-agents` before filing
  tickets that need an owner agent.
- Use `terminal-cli mcp request_agent_artifact` or
  `.claude/bin/request-agent-artifact` for focused cross-domain knowledge
  requests. Keep the returned artifact path and a short summary; do not paste
  long delegated transcripts into the parent prompt.
- At the end of implementation-style agents, file follow-up tickets for any
  deferred cross-agent work. Each follow-up gets exactly one `agent_id`,
  `agent_scope`, and `agent_kind`; multi-phase work becomes linked tickets.
- Use `terminal-cli hitl` only for true human blockers.
- Treat HITL as append-only: do not edit `hitl.json` or resolve it from the
  script. After filing, exit only if that lane is blocked; otherwise continue
  independent work and periodically re-check the blocker or query HITL status.
- Exit non-zero on real failure; `exit 0` for no-op success.

## Sidecar JSON

```json
{
  "id": "kebab-case",
  "title": "Short label",
  "description": "One-line summary",
  "icon": "Bot",
  "opensPr": false,
  "engine": "claude",
  "model": "haiku",
  "modelPolicy": {
    "default": "haiku",
    "cheap": "haiku",
    "deep": "sonnet",
    "judge": "gpt-5-mini",
    "allowOverride": true
  },
  "inPlace": false,
  "outputContract": "Human-readable summary plus ticket/PR/artifact links.",
  "quality": {
    "acceptanceCriteria": [
      "The agent completes its declared task or reports why it could not.",
      "The run ends with checks performed, artifacts produced, and follow-up ticket ids or none."
    ],
    "requiredArtifacts": [],
    "deterministicChecks": [],
    "judge": {
      "enabled": false,
      "mode": "deterministic",
      "rubric": [
        "Output matches the agent purpose.",
        "No unrelated changes were made."
      ]
    }
  }
}
```

Set `opensPr: true` only if the agent is expected to commit and open a PR/MR.
`engine` may be `"claude"`, `"codex"`, or `"cursor"`; use the user's selected
engine unless the agent has a clear reason to prefer one. For recurring
report/precheck agents, prefer cheap model defaults (`haiku`,
`composer-2.5-fast`, or a small Codex model such as `gpt-5-mini`). Leave
`model` empty only for implementation-heavy agents where the engine's coding
default is intentional.
Set `inPlace: true` only for agents that intentionally mutate the current repo
without a worktree; most agents should leave it false.
Set `quality.deterministicChecks` for commands that can verify output
mechanically. Enable `quality.judge` only when deterministic verification is
not enough; include a concrete rubric and judge model in `modelPolicy.judge`.

## Confirmation

End with the created/updated script path, sidecar path, and how to run it from
TerMinal.

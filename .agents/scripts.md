# scripts — the unified executable (design proposal, supersedes pipelines.md)

## The pivot

Today TerMinal has three abstractions for "run something":

- **Agent**: a `prompt` string wrapped at run-time as `claude -p $prompt`,
  `codex exec ... $prompt`, or `cursor-agent -p ... $prompt`.
- **Schedule**: launchd wrapping an agent on a cadence.
- **Pipeline** (proposed): a YAML graph of script + llm steps with conditionals.

The pivot: collapse all three into **one** concept — an **executable file**
(typically bash) that the runner just runs. The script chooses internally
whether to use `claude -p`, `codex exec`, `cursor-agent -p`, deterministic
shell commands, or some mix. The "pipeline" is just a script with a few `if`
blocks.

This eliminates the agents-vs-pipelines distinction, gives operators the full
power of bash + their existing toolchain, lets schedules avoid paying for an
LLM when a precheck script can answer "all green" cheaply, and stays trivially
inspectable — you can `cat` an agent definition and read what it does.

## File layout

```
.agents/<id>.sh                              # per-repo executable
.agents/<id>.json                            # per-repo metadata (sidecar)
~/.config/TerMinal/scripts/<id>.sh           # global
~/.config/TerMinal/scripts/<id>.json         # global metadata
```

Sidecar metadata stays as JSON for trivial parsing in main; the script body
is whatever bash needs to be. Frontmatter inside the script is allowed but
not required — the sidecar JSON is authoritative.

### Metadata sidecar

```json
{
  "id": "health-then-fix",
  "title": "Health check + LLM fix",
  "description": "Cheap precheck, escalate to an agent engine only on failure.",
  "icon": "Activity",
  "opensPr": true,
  "inPlace": false,
  "engineHint": "claude",
  "modelHint": "haiku"
}
```

`engineHint` + `modelHint` are *suggestions* the script body honors via
`$TERMINAL_ENGINE` / `$TERMINAL_MODEL` env vars; the schedule or one-off
launch can override them. Recurring report/precheck agents should default to
cheap/fast models (`haiku`, `composer-2.5-fast`, or a small Codex model such as
`gpt-5-mini`) and reserve stronger defaults for implementation-heavy agents.

## The script body

A minimal "old-style" agent translates 1:1:

```bash
#!/usr/bin/env bash
set -euo pipefail
exec claude -p 'Act as the documentation agent ...' --permission-mode auto
```

A pipeline-style script with a conditional escalation:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Precheck — cheap, no LLM
if bunx tsc --noEmit -p tsconfig.json \
   && bun test --bail \
   && curl -fsS "${HEALTH_URL:-http://localhost:3000/healthz}" > /dev/null; then
  echo "Healthy — no LLM run needed."
  exit 0
fi

# Failed precheck — escalate
claude -p "The health check failed. Diagnose and either apply a safe fix and open a PR, or file a HITL with the failure context." \
  --permission-mode auto \
  --model "${TERMINAL_MODEL:-haiku}"
```

Before interpolating command output into an LLM prompt, cap it. Good defaults:
`tail -200 "$log"`, `head -100` for file lists, and
`rg -n -C2 "$query" ... | head -160` for snippets. Prefer passing paths and
letting the engine open one specific file over pasting whole files into the
prompt.

## Runner environment

The runner exports the following env vars before exec'ing the script:

```
TERMINAL_REPO        repo root (absolute)
TERMINAL_RUN_ID      run uuid
TERMINAL_AGENT_ID    id of this agent (used as the state key — see below)
TERMINAL_BRANCH      worktree branch (or "main" if inPlace)
TERMINAL_WORKTREE    worktree path (== TERMINAL_REPO if inPlace)
TERMINAL_ENGINE      hint from sidecar / schedule override
TERMINAL_MODEL       hint from sidecar / schedule override
TERMINAL_TICKET      ticket id, when invoked from a ticket context
TERMINAL_PR          PR/MR number, when invoked from a PR-tab agent
```

PATH is augmented to include the TerMinal CLI helpers:

```
~/.config/TerMinal/bin/terminal-cli ticket "<title>" "<body>"
~/.config/TerMinal/bin/terminal-cli hitl "<title>" "<action>"
~/.config/TerMinal/bin/terminal-cli activity "<kind>" "<title>" "<detail>"
~/.config/TerMinal/bin/terminal-cli notify "<message>"
~/.config/TerMinal/bin/terminal-cli listener enqueue '<json-envelope>'
~/.config/TerMinal/bin/terminal-cli state {get-sha,mark-main,get,set,set-sha}
```

So a script can file a HITL with `terminal-cli hitl "Auth keys missing" "rotate in 1password"` — no need to hardcode the JSON file location.
Treat this API as append-only from agent code: it writes Inbox, emits activity,
and pings Telegram; only the human/operator resolves the item. To wait, query
HITL status or periodically re-check the original blocker.

Use `terminal-cli listener enqueue` when a script should request follow-up work
without running it inline. It writes a durable JSON event to
`~/.config/TerMinal/automation-inbox/new/`; TerMinal's Schedules tab shows the
request moving through `processing/`, `done/`, `failed`, or `dead-letter` and
links listener-triggered runs. See the `listener-inbox` skill for the envelope
schema and allowed actions.

## Per-(repo, agent) state

A standing problem for any agent that runs on a cadence: **what's the cheapest way to know what changed since last time?** The answer is the harness-owned state sidecar at:

```
~/.config/TerMinal/agent-state/<repo-basename>/<TERMINAL_AGENT_ID>.json
```

The state file lives **outside the repo** so cron-fired runs never dirty the working tree. The harness owns three canonical fields; scripts can set arbitrary string keys beyond that.

| Field           | Set by                       | Meaning                                                         |
|-----------------|------------------------------|-----------------------------------------------------------------|
| `lastScannedSha`| `state set-sha` / `mark-main`| Tip of **main/master** at the end of the previous scan          |
| `lastScannedRef`| `state mark-main`            | Which ref produced that sha (e.g. `origin/main`)                |
| `lastRunAt`     | any write                    | ms epoch of the most recent state write                         |
| `lastRunId`     | any write (if `TERMINAL_RUN_ID` set) | run id that produced the last write                     |

### Canonical incremental-scan pattern

Most cadence agents follow the same shape — exit cheaply when nothing new, do the work, record where we scanned to:

```bash
set -uo pipefail

last=$(terminal-cli state get-sha)        # "" on first run

# Fetch main + decide range. On first run, look back 50 commits as the default
# "cold start" window — adjust to taste per agent (a watchdog might want a
# tighter window, a changelog generator a wider one).
git -C "$TERMINAL_REPO" fetch --quiet origin || true
head=$(git -C "$TERMINAL_REPO" rev-parse origin/main 2>/dev/null \
   || git -C "$TERMINAL_REPO" rev-parse origin/master 2>/dev/null \
   || git -C "$TERMINAL_REPO" rev-parse HEAD)
range="${last:-HEAD~50}..$head"

if [ "$head" = "$last" ]; then
  echo "no new commits since $last"
  exit 0
fi

# Whatever the agent does — diff inspection, LLM-escalation, etc.
git -C "$TERMINAL_REPO" log --oneline "$range" -- src/

# … work happens here …

# Record that we've now scanned through origin/main's current tip.
terminal-cli state mark-main
```

### Storing extra fields

Agents can persist anything else they need between runs — last-known coverage %, advisory feed timestamps, finding counts, etc. — under arbitrary keys:

```bash
terminal-cli state set lastCoveragePct 78.4
terminal-cli state set flakeCount 2
prev_pct=$(terminal-cli state get lastCoveragePct)
```

Values are JSON-decoded on read when possible, so booleans, numbers, and arrays round-trip correctly. Treat the state file as **best-effort cache, not source of truth** — if it's missing or stale, the agent should still produce a correct first-run answer (do the full scan, exit 0, mark-main).

## Schedule integration

A schedule entry references a `scriptId` (per-repo or global). The existing
`agentId` field stays as a compatibility shim that resolves to a script when
`.agents/<id>.sh` exists. The schedule still owns `engine`, `model`,
`spec`, `enabled`, `lastRun`, etc. The runner now does:

```bash
# inside bin/terminal-cron's runSchedule:
script="$repo/.agents/$id.sh"           # or global
[ -x "$script" ] || die "no script for $id"
script -q /dev/null env \
  TERMINAL_REPO="$repo" TERMINAL_RUN_ID="$run" \
  TERMINAL_ENGINE="$engine" TERMINAL_MODEL="$model" \
  PATH="$TERMINAL_BIN:$PATH" \
  "$script"
```

PTY wrapping (`script -q /dev/null`) stays so agent CLIs inside still
stream output to the run log.

## Compatibility & migration

Phase 1 (now):
- The current `.agents/agents.json` + `prompt` model continues to work.
- New: the runner checks for `.agents/<id>.sh` first; if found, executes it
  with the env above; if not, falls back to building the configured engine prompt command.

Phase 2:
- A `/migrate-agents` skill converts every entry in `.agents/agents.json`
  into a matching `.agents/<id>.sh` + `.agents/<id>.json` sidecar.
- The body of each `.sh` is just the single `claude -p` / `codex exec` / `cursor-agent -p`
  line built from the prompt + engine + model. After migration the json
  blob is just metadata.

Phase 3:
- `.agents/agents.json` deprecated; readAgents emits a warning when it sees
  one and suggests `/migrate-agents`.

## Designer UX

The designer modal stays the same: user describes what they want in natural
language; the selected engine authors the script. The new bit is the *output*:

- The designer writes both files: `<id>.sh` (executable, chmod 755) +
  `<id>.json` (metadata).
- The preview pane in the modal renders the script body with syntax
  highlighting and the metadata as a card. "Edit" opens the script in the
  Files tab; "Run" spawns it.
- Templates available from a small picker in the designer:
  - **Single LLM call** — the simplest, equivalent to today's agents.
  - **Precheck + escalate** — the health-check-style script above.
  - **Multi-step composition** — sequence of checks with shared state.
  - **Pure deterministic** — no LLM at all (eg. nightly stats roll-up).

A non-technical operator never has to look at the bash; the description
field + template choice cover the common cases.

## UI surface

Add a `Scripts` view in the existing Agents tab (a chip toggle: Agents /
Scripts), or rename Agents → Scripts once migration completes. Each row
shows: title, icon, description, recent runs, last status, **a "script
preview" expander** that shows the bash with syntax highlighting + the
sidecar metadata. Editing opens the file in the Files tab.

The Schedules tab is unchanged — schedules already point at an id; the
underlying file just changed shape.

## What this is not

- **Not** a workflow engine (Temporal, Argo) — single-machine, single-process,
  no retries / DAG semantics beyond what bash gives you.
- **Not** a replacement for `/check <kind>` — `/check` is still the
  convention for scheduled cadence inspections; the scripts that implement
  each kind move to `.agents/<kind>.sh` under this proposal but the
  contract (sole-writer, reports/, etc.) stays.
- **Not** a sandbox — Claude calls run in auto permission mode; Codex still
  uses `-s danger-full-access`. Trust boundary is unchanged.

## Phase plan

- **Stage A (this file).** Design + migration path locked.
- **Stage B.** Add the runner branch: if `.agents/<id>.sh` exists, exec it
  with the env vars + PTY wrap, capture its stdout/stderr to the run log.
- **Stage C.** Build `terminal-cli` (the helpers exposed inside scripts).
- **Stage D.** `/migrate-agents` skill + Scripts UI view.
- **Stage E.** Deprecate `.agents/agents.json` after a release cycle.

The runtime change is small (it's just `if [-x file] then exec`); the work is
mostly in the helpers + the migration skill + the docs.

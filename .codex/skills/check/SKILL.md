---
name: check
description: "Run a scheduled cadence agent from an .agents/<kind>.md spec — isolated worktree, SHA-gated no-op on re-runs, then reports findings or proposes a ticket+MR. Use on /check <kind>, a scheduled-agent run, or wiring a launchd schedule."
---

# /check — Scheduled cadence agents

## Fast path: TerMinal MCP tools for state + activity

When the `terminal-harness` MCP server is registered, use these at the head +
tail of any `/check <kind>` run instead of `terminal-cli state` shell calls:

- **`get_agent_state({repo, agent: '<kind>'})`** at the start — read
  `lastScannedSha` for the early-exit branch (no work if HEAD === last).
- **`set_agent_state({repo, agent: '<kind>', key: 'lastScannedSha', value: '<sha>'})`**
  at the end — record where we scanned through.
- **`emit_activity({kind: 'check', title: '<kind> · N findings', repo})`** when
  the run finishes (clean or findings).
- **`list_agents({repo})`** before filing escalations that need an owner.
- **`file_ticket(...)`** for any escalations from the run; include exactly one
  `agentId`, `agentScope`, and `agentKind`.
- **`file_hitl(...)`** for true blockers.

These replace the equivalent shell calls; the per-kind `.agents/<kind>.md`
delegation pattern below is unchanged.

---

Where the `code-review` agent gates a single PR, `/check` runs a **repo-level agent** on
the whole tree at `main` HEAD — the cadence work (drift audit, coverage
backfill, dependency hygiene, changelog maintenance, auto-docs, perf
benchmarks, dead-code) that's noise if run per-commit. Each agent is defined
by `.agents/<kind>.md`.

Two modes, declared in the kind's spec:

- **`report`** — read-only inspection. Writes an artifact, may file a ticket
  or HITL, but never edits source. Examples: dead-code, drift (when only
  shape changes found), perf, coverage (when only flakes / big-gap tickets).
- **`writer`** — opens a PR proposing changes within its declared sole-writer
  paths (per `.agents/owned.yml`). Examples: changelog, auto-docs, drift (for
  trivial-fix PRs), coverage (for net-new test files), deps-quality (for safe
  bumps + lint fixes).

**All modes share the same workflow shape**: own worktree → analyze → decide
[PR / ticket / HITL / activity-only] → artifact → state update. The merge to
`main` is **always** human-only (global §8).

## Usage

```
/check <kind>           # run one cadence agent
/check                  # list available kinds (the .agents/*.md cadence specs)
```

Available kinds = the `.agents/*.md` specs that describe cadence agents (i.e.
everything except the per-PR contracts `code-review.md`, `testing.md`, and the
forge adapter `forge.md`).

## Prefer script-first execution

If `.agents/<kind>.sh` exists and is executable, run it directly first. The
script owns deterministic prechecks, early exits, capped prompts, and any
engine escalation. This is the cheapest path and should be the default for
scheduled agents.

```bash
kind="<kind>"
TERMINAL_REPO="$PWD" \
TERMINAL_AGENT_ID="$kind" \
TERMINAL_WORKTREE="$PWD" \
TERMINAL_BRANCH="$(git branch --show-current 2>/dev/null || echo main)" \
TERMINAL_ENGINE="${TERMINAL_ENGINE:-claude}" \
TERMINAL_MODEL="${TERMINAL_MODEL:-haiku}" \
PATH="$HOME/.config/TerMinal/bin:$PATH" \
  ".agents/$kind.sh"
```

Do not also paste `.agents/<kind>.md` into an LLM prompt when the script
succeeds or intentionally no-ops.

## Fallback: delegate to Codex

If no executable script exists for the kind, delegate the run to Codex from the
repo root:

```bash
codex exec -s danger-full-access -C "$PWD" "Run the <kind> cadence agent following .agents/<kind>.md in this repo exactly. Honor the spec's mode (report or writer), early-exit fast path, sole-writer scope, ticket+MR workflow, and worktree isolation. Write the artifact to .TerMinal/reports/<kind>/<short-sha>.md in v2 repos (legacy v1: reports/<kind>/<short-sha>.md) per the contract. Never push directly to main."
```

`-s danger-full-access` is required for the worktree + push steps.

## Process (uniform across kinds)

### 1. Early-exit fast path

The agent's state lives **outside the repo** at
`~/.config/TerMinal/agent-state/<host>/<repo>/<kind>.json` so cron runs don't
churn the working tree. Read it first:

```bash
state="$HOME/.config/TerMinal/agent-state/$host/$repo/$kind.json"
last=$(jq -r '.lastScannedSha // ""' "$state" 2>/dev/null)
head=$(git rev-parse HEAD)
if [ "$head" = "$last" ]; then
  echo "no new commits since $last — skip"
  exit 0
fi
```

Each kind may have additional fast-path conditions (changelog: "no new merges
since last," perf: "no benchmark script," etc.) — defined in its spec.

### 2. Worktree isolation

Every run gets its own worktree at
`${WORKTREES_DIR:-$HOME/.worktrees}/<repo>/check-<kind>-<short_sha>/`. This is
mandatory for parallel scheduling — multiple `/check` runs (different kinds,
or the same kind across repos) can fire concurrently from launchd without
corrupting each other's git state.

```bash
wt="${WORKTREES_DIR:-$HOME/.worktrees}/<repo>/check-<kind>-$short_sha"
git worktree add "$wt" main
cd "$wt"
```

### 3. Resolve + run

Read `.agents/<kind>.md` for the spec. Execute the kind's analysis per the
spec. Honor its declared inputs, decisions table, and sole-writer scope (cross-
check `.agents/owned.yml`).

### 4. Decide

Per the spec's decision rules, choose for each finding:
- **auto-PR** (writer mode only) — branch `<kind>/<short_sha>`, commit,
  `git push -u origin`, open PR via `.claude/skills/pr-creation` (forge-aware).
- **ticket** — `.claude/skills/ticket` with the artifact path attached.
- **HITL** — `.claude/bin/hitl "<title>" "<action needed>"` for true blockers
  (Critical CVE, ADR contradiction the agent can't resolve, etc.).
- **activity-only** — for findings worth surfacing but not actionable yet.

### 5. Write the artifact

`.TerMinal/reports/<kind>/<short_sha>.md` per the kind's frontmatter schema
(legacy v1: `reports/<kind>/<short_sha>.md`). Always
write the artifact, even on `status: ok` with no findings — the artifact is
the run record.

### 6. Update state

```bash
mkdir -p "$(dirname "$state")"
jq -n --arg sha "$head" --arg now "$(date +%s)000" \
  '{lastScannedSha: $sha, lastRunAt: ($now | tonumber)}' > "$state"
```

Kinds may persist additional fields (coverage: `lastCoveragePct`, perf:
`baseline.json` separately, etc.) — see each spec.

### 7. Clean up worktree

- If a PR was opened: leave the worktree for fix iteration on review.
- Otherwise: `git worktree remove "$wt"`.

### 8. Activity

```bash
.claude/bin/activity check "Check · <kind> · <summary>" "@ <short_sha>"
```

## Hard rules

1. **Ticket + MR workflow.** Every change goes through a PR. Direct push to
   `main`/`master` is blocked by the hook (global §8).
2. **Worktree isolation, always.** Even read-only runs use a worktree so
   parallel scheduling is safe.
3. **Sole-writer scope enforced.** A `writer`-mode agent only edits paths
   declared for it in `.agents/owned.yml`. Cross-scope edits → ticket, not PR.
4. **Idempotent.** Re-running on the same HEAD must be a fast no-op (early-
   exit check).
5. **No silent failures.** Missing tool / no benchmark / etc. → artifact with
   `status: not-configured` or `status: error` + a one-line reason.
6. **One run per invocation.** Don't retry-to-green; honesty over green.

## Parallel scheduling

Each kind is scheduled independently via TerMinal's Schedules tab (or
`schedules.json`). Multiple schedules can fire concurrently — the worktree
naming convention (`check-<kind>-<sha>`) keeps them isolated. State files are
per-(repo, kind), so concurrent runs of different kinds never collide.

A simple lock to prevent two concurrent runs of the *same* kind:

```bash
lock="$state.lock"
[ -f "$lock" ] && { echo "already running (lock: $lock) — skip"; exit 0; }
trap 'rm -f "$lock"' EXIT
touch "$lock"
```

## What this is NOT

- **Not a per-PR review.** Use the `code-review` agent for that.
- **Not a scheduler.** TerMinal's Schedules tab + launchd own scheduling; this
  skill just runs the agent and writes the artifact.
- **Not a writer for arbitrary paths.** Writer-mode agents are scoped to their
  declared sole-writer paths in `owned.yml`.

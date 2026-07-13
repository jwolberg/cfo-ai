# auto-docs agent (in-repo contract)

A scheduled agent that maintains a **tripartite docs structure** under `docs/`:

- **`docs/maintainer/`** — for contributors/maintainers. Architecture overview,
  IPC/API references, code conventions, internal module catalog.
- **`docs/developer/`** — for end-users/integrators. Getting started, public API
  reference, integration recipes, examples.
- **`docs/personal/`** — for the user's own context. "What shipped this week,"
  cycle-time trend, decisions log, active-branches snapshot.

The agent is **sole writer** of these three folders per `.agents/owned.yml`.
**Workflow is uniform**: own worktree → analyze → propose changes via a PR.
Human-authored docs belong elsewhere (e.g., `docs/runbooks/`, root
`docs/architecture.md`); the tripartite subfolders are regenerated content.

## Mode

`writer` — opens a PR amending its owned paths.

## Inputs (per category)

**Maintainer** (`docs/maintainer/`):
- Source files matching language patterns (`src/**/*.{ts,tsx,py,go,rs}`).
- IPC handlers (TypeScript: `ipcMain.handle\(`).
- Tab/plugin/widget catalogs (TerMinal pattern: `src/renderer/src/tabs/`, etc.).
- Module-level doc comments / top-of-file JSDoc.

**Developer** (`docs/developer/`):
- Exported public-API surface (TypeScript `export` declarations from index files).
- README sections matching feature blocks.
- Example folders (`examples/`, `samples/`).

**Personal** (`docs/personal/`):
- `git log <lastScannedSha>..HEAD` (what shipped).
- Closed tickets since `lastScannedSha` (from `.TerMinal/backlog/`).
- ADRs added/changed (from `docs/decisions/`).
- Cycle-time + factory health rollup (read from TerMinal's stores if available).
- Open branches + PRs snapshot.

## Early-exit fast path

State at `~/.config/TerMinal/agent-state/<repo-basename>/auto-docs.json`:

```json
{ "lastScannedSha": "abc1234", "lastRunAt": ..., "filesGenerated": 9 }
```

Early-exit conditions:
- `HEAD == lastScannedSha` → exit 0.
- No changes to source files matching the input globs since `lastScannedSha`
  AND no new closed tickets / merged PRs → exit 0.

The "personal" category may want to regen even without code changes if a new
ticket closed or a new ADR landed — check each category independently.

## Process

1. **Worktree**: `git worktree add "${WORKTREES_DIR:-$HOME/.worktrees}/<repo>/auto-docs-<short_sha>" main`.
2. **Plan per category** — determine which of the three categories need regen.
3. **For each category that needs regen**:
   - Generate the agent-owned files for that category (list below).
   - Preserve each file's **managed-by header**.
4. **Diff against `main`** — if no files actually changed, exit (state-updating no-op).
5. **Open PR** with `--base main` and title `docs: auto-regen <category list>`.
   Branch: `auto-docs/<short_sha>`. Body: per-file regen reason summary.
6. **Update state** — `lastScannedSha = HEAD`.
7. **Activity** — `.claude/bin/activity doc "Auto-docs · <N> files regenerated" "<categories>"`.

## Owned files (per category, declared in `owned.yml`)

**Maintainer** (`docs/maintainer/`):
- `architecture.md` — high-level shape (layers, data flow, key seams).
- `api-reference.md` — exported types and functions, grouped by module.
- `ipc-reference.md` — every `ipcMain.handle` channel: name, params, returns.
- `module-catalog.md` — one-line summary of every top-level source file.
- `tab-catalog.md` (TerMinal-specific; only if `src/renderer/src/tabs/` exists).

**Developer** (`docs/developer/`):
- `getting-started.md` — install + first run from README's Quick start.
- `public-api.md` — public exports only (not internal).
- `recipes.md` — common integration patterns from `examples/` or README.

**Personal** (`docs/personal/`):
- `weekly-summary.md` — what shipped (merges, closed tickets) since last run.
- `decisions-log.md` — ADRs in chronological order with one-line summaries.
- `active-work.md` — open branches + PRs with status + age.
- `cycle-time.md` (TerMinal-aware; only if factory-health is reachable).

## Decisions

| Condition | Action |
|---|---|
| Category has no input changes | Skip that category's files. |
| Any file's regenerated content differs from `main` | Include in the PR. |
| Source for a doc removed (e.g., a module deleted) | Remove the corresponding doc file in the PR (sole-writer scope only). |
| Cycle-time data unavailable | Skip `cycle-time.md`, log to artifact. |

## Output artifact

`.TerMinal/reports/auto-docs/<short_sha>.md` — frontmatter + per-category regen summary.

```yaml
---
kind: auto-docs
generated: 2026-06-01T08:00:00Z
sha: abc1234
last_scanned: 9b3de89
categories_regenerated: [maintainer, personal]
files_changed: 6
pr_opened: https://github.com/owner/repo/pull/N
status: ok
---
```

## Hard rules

1. **Sole writer of `docs/maintainer/`, `docs/developer/`, `docs/personal/`** —
   never touch other paths.
2. **Ticket + MR workflow** — never push to `main`.
3. **Worktree isolation** — every run gets its own.
4. **Preserve managed-by header** on every regenerated file.
5. **Idempotent** — re-running with no input changes is a fast no-op.

## Managed-by header

Every regenerated file starts with:

```markdown
<!-- managed by: auto-docs · do not edit by hand · regenerated when source changes -->
```

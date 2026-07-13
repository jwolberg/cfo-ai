---
name: document-audit
description: "Audit docs/ and ~/.claude/docs/ for rot — broken refs, stale runbooks, ADR contradictions, architecture drift, orphan CLAUDE.md. Reports only; never auto-edits. Use on /document-audit or 'check doc health'."
---

# /document-audit — Catch documentation drift

Walk all docs and report rot. The user decides what to prune, archive, or update — this skill never edits or deletes on its own.

## What to check

For each `.md` under `docs/` (current repo) and `~/.claude/docs/` (global):

1. **Broken refs** — file paths, function names, package names, or commands mentioned in the doc that no longer exist in the referenced repo.
2. **Stale runbooks** — `last-verified` older than 90 days.
3. **ADR contradictions** — two `accepted` ADRs whose decisions appear to conflict, where neither has a `supersedes` / `superseded-by` link.
4. **Architecture drift** — `architecture.md` mentions folders, services, or major deps that no longer match the repo state.
5. **Orphan per-folder CLAUDE.md** — file exists in a folder that has been deleted or emptied.
6. **Missing per-folder CLAUDE.md** — *do not* flag this. Per-folder summaries are opt-in; absence is not rot.

## Report format

Group findings by severity:

- **Broken** (definitely wrong): broken refs, orphan CLAUDE.md files
- **Stale** (likely wrong): runbooks past 90 days, architecture drift
- **Suspect** (worth a human read): ADR contradictions

For each finding, include:
- File path (relative)
- The specific issue (one line)
- Suggested action: `update` / `archive` / `delete` / `mark superseded`
- For broken refs: the broken token (file path / symbol / command)

End the report with totals per severity.

## What NOT to do

- **Do not edit or delete docs.** Report only.
- **Do not flag style/formatting issues** — only correctness and rot.
- **Do not flag** ADRs with `status: deprecated` or `status: superseded` — those are expected and shouldn't appear as contradictions.
- **Do not flag** runbooks without `last-verified` as stale — flag them as missing-frontmatter (Suspect) instead.
- **Do not flag** missing per-folder CLAUDE.md files. They're opt-in.

## Scope control

If both project `docs/` and `~/.claude/docs/` are in scope and the report would be very long, ask the user which scope to audit first. Otherwise audit both and report together with clear path prefixes.

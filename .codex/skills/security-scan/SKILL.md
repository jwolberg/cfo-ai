---
name: security-scan
description: "Deterministic local security checks — dependency CVE audit (bun/npm) by default, optional SAST (semgrep) and opt-in gitleaks — summarized for code-review's Security axis. Use on /security-scan or as a pre-push gate."
---

# /security-scan — Deterministic security checks

Three layered checks, run in order. Each one is *deterministic* — same code, same result. Fills the gap that human-applied security checklists leave (which is "I forgot to look at X").

The Security axis in the `code-review` agent is only as good as what gets checked. This skill makes the floor reproducible.

## Inputs

- `repo_path` — local checkout to scan. Default: cwd.
- `base_ref` (optional) — for diff-range scans, e.g., `origin/main`. Default: scan whole tree.
- `mode` — `full` (whole repo) or `diff` (only changes since `base_ref`). Default: `diff` if `base_ref` is set, else `full`.
- `--with-gitleaks` (flag) — opt in to secret scanning. **Off by default** — gitleaks against a fork's full history can take minutes (14k upstream commits = 3 min, with most "leaks" being upstream noise). When in doubt run gitleaks separately, scoped: `gitleaks detect --log-opts="origin/master..HEAD"`.

## Checks

### 1. Dependency CVE audit (always)

Run the package manager's audit command. Detect from project files:

| Project file | Command |
|---|---|
| `bun.lock` / `bun.lockb` | `bun audit` |
| `package-lock.json` | `npm audit --audit-level=moderate` |
| `pnpm-lock.yaml` | `pnpm audit --audit-level=moderate` |
| `yarn.lock` | `yarn audit --level moderate` |
| `Cargo.lock` | `cargo audit` (if `cargo-audit` installed; else suggest install) |
| `pyproject.toml` + `uv.lock` | `uv run pip-audit` (if available) |
| `requirements.txt` | `pip-audit -r requirements.txt` |
| `Gemfile.lock` | `bundle audit` |
| `go.sum` | `govulncheck ./...` (if available) |

Capture:
- Number of advisories at each severity (low / moderate / high / critical)
- Top 5 highest-severity entries with package name, version, and CVE/advisory ID
- The fix command if the tool provides one (`bun audit --fix`, etc.)

### 2. Secret scanning (gitleaks) — OPT-IN

**Skipped by default.** Gitleaks is fast on a clean repo (12 commits → 70ms)
but slow on forks with deep upstream history (14k commits → 3+ minutes,
mostly upstream noise that isn't actionable). Running it on every
the `code-review` agent adds minutes for low marginal value.

Run only when `--with-gitleaks` is passed, or out-of-band:

```bash
# Recommended ad-hoc invocation — scope to PR commits only:
gitleaks detect --no-banner --redact --source "$repo_path" --log-opts="${base_ref:-origin/master}..HEAD"

# Repo-wide (slow on forks; expect false positives from upstream history):
gitleaks detect --no-banner --redact --source "$repo_path"
```

When run, capture:
- Number of leaks found
- File paths and rule IDs (e.g., `aws-access-key`, `slack-token`, `generic-api-key`)
- Commit SHA and line range for each
- **Never** include the redacted secret value in the output — just rule and location.

When skipped (default), emit:

> Secret scanning skipped (gitleaks is opt-in). Run `gitleaks detect --log-opts="origin/master..HEAD"` ad-hoc, or pass `--with-gitleaks` to include in this scan.

Do not deduct from the Security score for the skip — it's the default, not a missing-tool gap. Only deduct when `--with-gitleaks` was requested AND gitleaks isn't installed.

### 3. SAST (semgrep, optional)

If `semgrep` is on PATH and the user has not opted out via `SECURITY_SCAN_NO_SAST=1`:

```bash
semgrep --config=auto --json --no-git-ignore "$repo_path" > /tmp/semgrep-report.json
```

Capture top findings by severity (ERROR / WARNING / INFO). Filter out test files unless they're the only files in the diff.

If semgrep is NOT installed, emit a one-liner noting it and continue. Don't suggest installing it unless the user has expressed interest in deeper SAST — semgrep is a heavier tool and not always worth the setup.

## Output format

```markdown
## /security-scan results

**Repo:** `<path>` · **Mode:** `<full|diff>` · **Base:** `<ref or n/a>`

### Dependency audit (<package manager>)
- Critical: N
- High: N
- Moderate: N
- Low: N

<Top 5 advisories, one bullet each: package@version → CVE ID, severity, summary, fix-version if known>

### Secret scanning (gitleaks)
- Status: <ran | skipped (default — opt in with --with-gitleaks) | skipped (not installed) | error>
- Leaks found: N
<For each leak: rule_id · file:line · commit_sha (if diff mode)>

### SAST (semgrep)
- Status: <ran | skipped (not installed) | skipped (opted out)>
- Errors: N · Warnings: N · Info: N
<Top 5 ERROR-level findings, one bullet each: rule_id · file:line · short description>

### Score recommendation for the code-review agent

Security axis suggested score: NN/100

Anchored to:
- <which findings drove the deduction>

If the user wants to override, they can. This is the floor — manual review can find more (auth flow bugs, business logic, etc.) but cannot find less than this scan caught.
```

## Score recommendation rubric

The skill suggests a Security axis score so the `code-review` agent can incorporate it:

- **Critical CVE in new/changed dep**: -40
- **High CVE in new/changed dep**: -20
- **Moderate CVE in new/changed dep**: -10
- **Pre-existing CVEs not introduced by this PR**: don't deduct (note them, but they aren't this PR's problem)
- **Any leaked secret** (when gitleaks is run): -50 (and this is severity=critical regardless — a leak shipped to git is already a security incident)
- **gitleaks skipped (default)**: 0 — not a deduction. Manual review still applies the secrets section of the security checklist.
- **gitleaks requested but not installed**: -5 (uncertainty discount)
- **Semgrep ERROR finding in changed files**: -10 each
- **Semgrep WARNING finding in changed files**: -3 each

Floor at 0; cap at 100. The `code-review` agent is free to override based on manual reading (e.g., auth bypass that no automated tool would catch).

## What this skill is NOT

- **Not a replacement for manual security review.** Auth flows, authz logic, business-rule bypasses, and design-level security decisions need a human or LLM reading the code. This skill catches the deterministic floor.
- **Not a fixer.** It reports findings; another skill (or human) applies fixes.
- **Not stateful.** Each run is independent. No caching, no historical tracking.

## Hard rules

- **Never echo redacted secret values in chat or artifacts.** Use rule_id and location only.
- **Never disable `--redact`** in gitleaks output, even for debugging.
- **If a leaked secret is found, halt the calling workflow.** /pr-creation must not push a branch with detected leaks. The user has to rotate the secret and rewrite history before any push.

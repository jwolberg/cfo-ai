# code-review agent (in-repo contract)

Reviews a single PR at a specific commit and emits **one combined artifact**
containing both the test-run summary and the structured findings. Artifacts
live **in this repo** under `.TerMinal/reviews/<pr-number>/` in v2 repos
(legacy v1: `.reviews/<pr-number>/`) — versioned with the code, no external
dashboard or central harness.

Aim: a Greptile-style review — broad in coverage, conservative in confidence,
explicit about what *evidence* led to each finding, and **scored on six axes**
so reviewers (human or agent) can see at a glance which dimension is weak.

Treat every PR as if it ships to production at scale, even small ones.
Security and architecture are first-class scoring axes alongside correctness.

## Token-efficient reading order

This contract is ~400 lines. To minimize context, read only what your current
task needs:

| If you're … | Read these sections only |
|---|---|
| **Scoring axes** | "Score the six axes" + the relevant axis subsection |
| **Writing the artifact** | "Output location" + "Artifact frontmatter (required)" + "Artifact body" |
| **Computing findings** | "Per-finding state" — but the harness helper `.claude/bin/findings-merge` handles state mgmt; you just emit fresh findings in a fenced ```findings-new ... ``` block at the end of the artifact body |
| **Deciding verdict** | DO NOT — the helper `.claude/bin/compute-verdict` derives it deterministically from scorecard + findings + test_status. Your job is the scoring + findings; verdict is rule-driven |
| **Stacked-MR batch** | "Batch stacked-MR review" |
| **Severity rules** | "Severity-based body rules" |

The preflight script `.claude/bin/code-review-preflight` has already done all
the recon (PR metadata, file list, language histogram, surface flags, prior
findings count, diff, test results). Read the packet path passed by the skill;
don't redo this work.

## Output location

```
.TerMinal/reviews/<pr-number>/<short_sha>.md     # one file per commit reviewed
.TerMinal/reviews/<pr-number>/findings.json      # canonical per-finding state
.TerMinal/reviews/<pr-number>/suggestions.json   # canonical per-suggestion state
```

`<short_sha>` is the first 7 hex chars of the head commit. `<pr-number>` is the
change number — the GitHub PR number or GitLab MR number (no host/owner prefix;
the artifacts are in the repo they describe). Forge resolved via
[`forge.md`](./forge.md).

## One artifact per reviewed commit

A `/code-review` run writes exactly one full six-axis artifact for the head
commit it reviewed (`kind: review`). Re-running on the same SHA overwrites in
place. Cheap "did tests stay green?" checks between reviews are handled by
`/test-suite` (chat-only, no artifact) — there is no separate tests-only
artifact kind in this workflow.

## Incremental re-review discipline

A later review in the same PR should **not** re-litigate the entire PR unless a
new commit invalidates earlier assumptions.

1. Treat the prior review artifact, `findings.json`, and `suggestions.json` as
   trusted state unless the new diff touches the same behavior.
2. Identify the review base as the newest earlier reviewed commit still in the
   PR. Inspect `git diff <reviewed_sha>..HEAD` first.
3. Classify the iteration: **fix-only / narrow** (verify the fix + regression
   coverage, carry scores forward), **mixed** (review new behavior, reuse
   unaffected conclusions), or **reset-worthy** (core rewrite — full review,
   say why).
4. Don't paste long prior findings into the new artifact. Resolved/stale state
   lives in `findings.json`; the body discusses only current open findings, new
   findings, and the re-review delta.

Tests remain non-negotiable: every `/code-review` runs the detected suite for
the current HEAD regardless of scope.

## Batch stacked-MR review

In `/stacked-mr` mode review is **batched**: the stack is built without per-PR
review, then one pass reviews every PR in the stack. The batch is **N independent
single-PR reviews run concurrently**, not a new combined format:

- **One artifact set per PR, unchanged.** Each PR still gets its own
  `.TerMinal/reviews/<pr>/<sha>.md` + `findings.json` + `suggestions.json`
  (legacy v1: `.reviews/<pr>/<sha>.md`), keyed by that PR's number and head SHA.
  There is no combined batch artifact.
- **Attribution = the PR's own incremental slice.** Each review resolves its base
  from the PR's forge target branch (the parent PR's branch in a stack), so it
  sees only that PR's delta and attributes findings to the owning PR. This is the
  per-PR incremental base, distinct from the "newest earlier reviewed commit"
  base used when re-reviewing one PR over time.
- **Isolation is mandatory.** Each concurrent review runs in its **own worktree**
  at that PR's branch tip. Reviews inspect and test a checkout; sharing one
  working tree across parallel reviews corrupts git state and cross-contaminates
  results.
- **Single-URL invariant holds.** `/code-review` (and `bin/code-review-preflight`)
  take exactly one PR/MR per invocation. "Batch" is the orchestrator firing N
  invocations at once (one per PR) — see the `/stacked-mr` skill. The test gate,
  six-axis scoring, and verdict logic per PR are unchanged.
- **The bar is unchanged.** Batching changes *when* and *how* reviews run, not
  the merge bar: every PR still needs approve + tests pass + 0 medium+ findings.

## Per-finding state (`findings.json`)

Every finding has a **stable id** that survives across iterations:

```
id = sha256(title + "|" + file).slice(0, 8)
```

Schema:

```jsonc
{
  "pr": "<owner>/<repo>#<number>",
  "updated_at": "<ISO 8601>",
  "findings": [
    {
      "id": "<8-char hex>",
      "title": "<finding title>",
      "severity": "critical|high|medium|low",
      "category": "<category>",
      "file": "<path:line>",
      "confidence": <int 1-10>,
      "first_seen_sha": "<short>",
      "first_seen_at": "<ISO 8601>",
      "status": "open|resolved",
      "status_changed_at": "<ISO 8601>",
      "status_changed_by": "auto:code-review",
      "resolved_in_sha": "<short>|null"
    }
  ]
}
```

- `/code-review` reads existing `findings.json`; matches new findings by `id`;
  carries `first_seen_sha`/`first_seen_at` forward on matches; marks prior
  findings absent from the new scan `status: resolved` with
  `resolved_in_sha = <current short>`.

## Per-suggestion state (`suggestions.json`)

Non-blocking notes: small improvements, cleanup, follow-up ideas worth
preserving but which must not affect verdict, score, or merge readiness. Do
**not** put correctness, security, architecture, test-gate, or policy failures
here — those are findings.

```jsonc
{
  "pr": "<owner>/<repo>#<number>",
  "updated_at": "<ISO 8601>",
  "suggestions": [
    {
      "id": "<8-char hex>",
      "title": "<suggestion title>",
      "category": "<category>",
      "file": "<path:line>",
      "confidence": <int 1-10>,
      "first_seen_sha": "<short>",
      "first_seen_at": "<ISO 8601>",
      "status": "open|accepted|dismissed",
      "status_changed_at": "<ISO 8601>",
      "status_changed_by": "auto:code-review",
      "resolved_in_sha": "<short>|null"
    }
  ]
}
```

## Artifact frontmatter (required)

```yaml
---
pr: <owner>/<repo>#<number>
commit: <full 40-char sha>
short_sha: <7-char>
kind: review
generated: <ISO 8601 datetime>
generator: <stable id>          # e.g. codex:gpt-5
verdict: approve | request-changes | blocked
summary: "<one-line headline, <=120 chars — quote it>"
review_scope: full | mixed | narrow
review_base_sha: <7-char|null>
test_status: pass | fail | partial | error | missing
test_runner: <bun test, pytest, cargo test, ...>
test_command: "<exact command run>"
test_exit_code: <int>
test_duration_seconds: <float>
test_counts:
  passed: <int>
  failed: <int>
  skipped: <int>
  total: <int>
scores:
  correctness: <0-100|null>
  security: <0-100|null>
  architecture: <0-100|null>
  conformance: <0-100|null>
  quality: <0-100|null>
  dependencies: <0-100|null>
  overall: <0-100>              # MIN of the six — weakest-link
findings_count: <int>
suggestions_count: <int>
avg_confidence: <float, one decimal>
---
```

Always quote the `summary:` value — unquoted strings containing `: ` break YAML
front-matter parsing.

## Artifact body

```markdown
## Summary

<2-4 sentences: what changes, what risks, recommendation. For incremental
reviews, name the review base + scope in one sentence.>

## Tests

Terse-on-pass: structured metadata lives in frontmatter. On pass, ONE line:
> `bun test` → 9/9 pass in 0.42s. New: `tests/oauth/cookie.test.ts` (4 cases).

On fail/partial: enumerate failures (test name, file:line, key error excerpt) +
a "Raw test output (last 8 KB)" block. This is an early-exit: write the blocked
artifact and STOP — do not score axes or run /security-scan.

## Scorecard

| Axis | Score | Notes |
|---|---|---|
| Correctness | NN/100 | <one-line rationale> |
| Security | NN/100 | <one-line rationale> |
| Architecture | NN/100 | <one-line rationale> |
| Conformance | NN/100 | <one-line rationale> |
| Quality | NN/100 | <one-line rationale> |
| Dependencies | NN/100 | <one-line rationale, or "n/a (no dep changes)"> |
| **Overall** | NN/100 | min of above |

## Findings

Per finding (match section depth to severity):

### 1. <Title>  [id: NNNNNNNN  ·  confidence: N/10]

- **Severity:** critical | high | medium | low
- **Category:** bug | security | performance | architecture | maintainability | style | testing | docs | conformance | dependency
- **File:** `path/to/file.ext:line`
- **Score impact:** <which axis, e.g. "correctness -15">

**Definition** (critical/high; medium only if title is opaque)
**Reproduction** (critical/high; medium only if non-obvious)
**Fix prompt** (ALWAYS — copy-pasteable into a fresh LLM session, names files +
desired end state + constraints)
**Regression test** (critical/high only)

Group several lows under a single `### Minor / nits` bullet list (each still
gets a `findings.json` entry).

## Suggestions

Non-blocking notes tracked in `suggestions.json`. If none: "No suggestions."
Out-of-scope ideas that need real follow-up work get filed as backlog tickets
(see "Filing out-of-scope follow-ups" below) and referenced by id here.
```

### Early exit on red tests

If `test_status != pass`: emit a `kind: review` artifact with
`verdict: blocked`, full `test_*` frontmatter, `## Tests` failure detail, and a
single high-severity testing finding with a copy-pasteable fix prompt. Set
`scores.overall: 0` and every other axis `null`. Do NOT run /security-scan,
score axes, or mutate prior `findings.json`/`suggestions.json` state.

## Confidence rubric (per finding)

- **9–10:** Definite — traced end-to-end.
- **7–8:** Strongly suspected.
- **5–6:** Code smell / maintainability. Style nits NEVER above 6.
- **3–4:** Speculative.
- **1–2:** Open question — usually drop instead of filing.

## Category scoring rubric (per axis, 0–100)

- **95–100:** Exemplary, positive evidence of quality.
- **85–94:** Solid, minor nits at most.
- **70–84:** Acceptable but has medium-severity findings.
- **50–69:** Concerning — multiple medium or one high.
- **0–49:** Significant problems.

**Overall = MIN of the six.** An axis that doesn't apply (e.g. no dep changes)
is `null` and excluded from the min.

## Security checklist (Security axis)

**Run the deterministic floor first.** On the green-tests path, run `/security-scan`
in diff mode (dependency CVE audit; optional gitleaks secret scan + semgrep SAST —
see `.claude/skills/security-scan`). Take the **lower** of its recommended Security
score and your manual read below; any leaked secret is an automatic critical →
`blocked`. Then apply the checklist:

Each gap knocks Security 10–40 by severity. Input validation at every boundary
(zod for requests/env/file/LLM output; watch SQLi, XSS, command injection,
SSRF, path traversal, ReDoS). Authentication on every protected endpoint.
Authorization on the *specific resource* (watch IDOR). No hardcoded secrets —
env vars only, none in logs/errors/client code. Vetted crypto only (no
home-rolled, no MD5/SHA-1 for security, no static IV/nonce). New deps checked
for CVEs. HTTPS for external calls, TLS verification on. CORS not `*` for
credentialed endpoints; CSRF tokens or SameSite cookies on state-changing
routes. Rate-limit public/auth/write paths. No stack traces or internals leaked
to users. File uploads: content-type validated, size-limited, stored outside
web root. Regulated data (PII/HIPAA/PCI): encrypted at rest+transit, access
logged, least privilege. Security headers (CSP, HSTS, X-Frame-Options,
X-Content-Type-Options). A violation with confidence ≥ 7 is **critical** or
**high**, almost never medium.

## Architecture checklist (Architecture axis)

Design consistency (match repo patterns; justify new abstractions). Latency
budget (p50/p95 within SLA; count new synchronous external calls). Performance
(N+1 queries, missing indexes, sync I/O in hot paths, O(n²) on large inputs).
Scalability (bounded memory, no unbounded caches, concurrency-safe, idempotent
on retry). Caching strategy + invalidation. Sync vs async (long ops async,
quick lookups sync; don't async speculatively). DB schema changes backwards
compatible (nullable/default columns, idempotent reversible migrations). Public
API changes backwards compatible (version if breaking). Service boundaries
respected. Observability at meaningful boundaries. A high-confidence regression
or scaling break is **high** or **critical**; stylistic nits stay **low** or drop.

**Reachability / wiring (tests passing ≠ shipped).** New behavior must be
reachable from a real **production entry point** — HTTP route, CLI command,
scheduled job/worker, UI event handler, exported package API, or contract
selector/ABI — not only from its own tests. Trace the call chain entry-point →
symbol. A symbol referenced **only** by tests/fixtures is **unwired**: the suite
is green but the feature doesn't run in production. New code the diff adds but
never wires to an entry point is a **high**-severity finding. The durable proof
of wiring is an **automated e2e/integration test** that exercises the real entry
point (see [`testing.md`](./testing.md)) — prefer that over a static trace alone.

## Anti-slop checklist (Quality axis)

Each knocks Quality 5–15: speculative flexibility (config nothing sets,
one-impl "extensible" abstractions), dead code introduced this PR,
over-abstraction (single-use helper/class/interface), WHAT-comments, vague
names (`data`/`result`/`helper`/`utils.ts`/`processItem`), unnecessary error
handling (try/catch around code that can't throw, validation of trusted
internal inputs), "just in case" null-checks the type system disproves, and
TODO/FIXME left in committed code without a linked ticket.

## Conformance checklist (Conformance axis)

Read project `CLAUDE.md` (root + any nested in touched folders) AND global
`~/.claude/CLAUDE.md`. Check library choices (zod/axios/native Date per global
§6), naming, idiomatic error handling (throw + boundary try/catch, no
Result/Either), `type` over `interface`, barrel `index.ts` per folder, feature
folders, WHY-only rare comments, ADR for non-obvious decisions, per-folder
`CLAUDE.md` for new folders, and **global §8** (any push/force-push/merge to a
protected branch → critical conformance finding).

## Dependency review checklist (Dependencies axis)

For each added dep: necessity (could be ~20 lines of vanilla code → finding),
trust signals (active maintenance, not abandoned, advisory history), bundle/
cold-start impact, license (flag GPL/AGPL/SSPL in non-copyleft projects), lock
file diff matches the manifest, and duplicates (two date libs, two HTTP
clients). Per global §10: deps pinned exact, lockfile committed, ≥ 3 days old.

## Verdict logic

- `blocked` — `test_status != pass`, OR a critical finding violates a global
  CLAUDE.md hard rule (especially §8 merge-to-main).
- `request-changes` — any critical finding, OR a high finding with confidence
  ≥ 7, OR `overall < 70`.
- `approve` — `test_status == pass` AND zero findings at severity ≥ medium AND
  positive evidence the change is safe (you can articulate *why*, not just that
  nothing was found). Note: this project's merge bar is verdict + 0 medium+
  findings + tests pass; the overall score is informational, not a gate.

## Filing out-of-scope follow-ups (the reviewer can create tickets)

A review often surfaces work that is real but **out of scope for this PR**: a
latent bug elsewhere, a refactor the change makes newly worthwhile, a missing
test surface, a follow-up extension, or tech debt the diff brushed against.
Don't drop these on the floor, and don't scope-creep the current PR to fix them.

**File a backlog ticket** so the idea is tracked. The reviewer has full access
to this repo's `/ticket` system:

```bash
ROOT="$(git rev-parse --show-toplevel)"
"$ROOT/.claude/skills/ticket/bin/tickets" open      # check for duplicates first
id=$("$ROOT/.claude/skills/ticket/bin/next-ticket-id")
# write .TerMinal/backlog/<id>-<slug>.md per .claude/skills/ticket/EXAMPLE.md
# (legacy v1 repos may use backlog/<id>-<slug>.md):
#   status: open, source: code-review, an appropriate type + priority,
#   and at least one concrete, testable acceptance criterion.
```

Reference the new ticket id in the artifact's **Suggestions** section (or in the
finding itself, if it's a real-but-deferred defect). Keep the *current* PR
focused; route everything else to the backlog. Don't file duplicates.

## Things to avoid

**Don't manufacture findings.** A reviewer prompted to find gaps will report
some even when the work is sound — resist it. A clean PR legitimately has **zero
findings**; "no issues found + positive evidence it's safe" is the correct
result, not a failure to look hard enough. Scope findings to correctness,
security, architecture, and stated requirements; preference-level observations
go to `suggestions.json`, never findings. The goal is an accurate review, not a
long one.

Also avoid: padding with low-confidence style nits; findings without file:line;
"would be nice" framed as a finding (put it in `suggestions.json`, or file a
ticket if it needs real work); re-flagging issues already raised at an earlier
SHA; filing duplicate tickets; approving without test verification (**never**).

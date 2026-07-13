---
name: code-review
description: "Compatibility launcher for the code-review agent. Delegates a GitHub/GitLab PR review to Codex: deterministic preflight does recon, then Codex scores six axes into one artifact at .TerMinal/reviews/<pr>/<sha>.md (v2) or .reviews/<pr>/<sha>.md (legacy v1). Use on /code-review or 'review this PR'."
---

# /code-review — PR review with six-axis scoring + embedded tests

This skill is a compatibility command for the `code-review` agent. Workflow
docs should refer to the agent; this command exists so `/code-review` still
launches the same reviewer contract.

Produces **one combined artifact** at `.TerMinal/reviews/<pr-number>/<short_sha>.md`
in v2 repos, or `.reviews/<pr-number>/<short_sha>.md` in legacy v1 repos.
The schema, scoring rubric, severity rules, and verdict logic live in
[`.agents/code-review.md`](../../../.agents/code-review.md).

Forge: **GitHub or GitLab**, auto-detected (`.claude/bin/forge`). Artifacts
are **in-repo** — no central dashboard.

## Workflow (three deterministic stages around one codex call)

```
    1. preflight (deterministic, ~5-30s, $0)
            ↓
       short-circuit?  →  copy artifact / write blocked / use lite path
            ↓
    2. codex exec (the deep review, ~3-4 min)
            ↓
    3. findings-merge + compute-verdict (deterministic, ~0s, $0)
```

**Stage 1 — preflight (always runs, free, fast):**

```bash
PACKET=$(./.claude/bin/code-review-preflight "$PR_URL")
EXIT=$?
```

The preflight script computes everything that doesn't need an LLM:
PR metadata, file list, language histogram, surface flags
(auth/migrations/routes/deps/lockfile/docs/etc.), diff_hash for cache lookup,
runs the test suite (with result caching), reads prior `findings.json` +
`suggestions.json`, and picks a `review_kind_hint` (`docs-only` /
`lockfile-only` / `code`). Writes the packet to `/tmp/review-packet-<sha>.json`.

**Stage 1.5 — check for short-circuit (no codex needed):**

If the preflight exits with code 2, the packet's `short_circuit` field tells
you what to do:

- **`already_reviewed`** — `.TerMinal/reviews/<pr>/<sha>.md` already exists for this
  exact SHA (or `.reviews/<pr>/<sha>.md` in legacy v1). Surface the existing artifact path; we're done.
- **`diff_hash_match`** — a prior SHA has bit-identical diff (rebase / amend
  / force-push without content change). Copy that artifact forward with a
  new SHA filename and `equivalent_to: <old_sha>` in the frontmatter; we're done.
- **`tests_red`** — fill the `blocked.md.tmpl` with test totals + tail and
  write it to `.TerMinal/reviews/<pr>/<sha>.md` (or `.reviews/<pr>/<sha>.md` in legacy v1). No codex invocation; verdict is
  deterministically `blocked`. We're done.

Sample (for `tests_red`):

```bash
REVIEW_ROOT=$([ -d .reviews ] && [ ! -f .TerMinal/template.json ] && echo .reviews || echo .TerMinal/reviews)
mkdir -p "$REVIEW_ROOT/$PR"

if [ "$EXIT" -eq 2 ]; then
  SC=$(jq -r '.short_circuit' "$PACKET")
  case "$SC" in
    already_reviewed) echo "✓ already reviewed: $(jq -r '.artifact' "$PACKET")"; exit 0 ;;
    diff_hash_match)  src=$(jq -r '.source_artifact' "$PACKET")
                      dst="$REVIEW_ROOT/$PR/$SHORT.md"
                      cp "$src" "$dst"
                      sed -i '' "s/^short_sha:.*/short_sha: $SHORT/; s/^commit:.*/commit: $HEAD/" "$dst"
                      echo "✓ diff-hash cache hit" ; exit 0 ;;
    tests_red)        ./fill-blocked-template.sh "$PACKET" > "$REVIEW_ROOT/$PR/$SHORT.md"
                      exit 0 ;;
  esac
fi
```

(Exit code 0 = continue to codex; 2 = short-circuit handled; 3 = bad URL;
4 = no test runner detected.)

**Stage 2 — codex exec (only if no short-circuit):**

Codex consumes the packet so it doesn't redo the recon. Pass the packet path
via the externalized prompt template `prompt.md`:

```bash
# Substitute placeholders into the prompt
sed "s|{{PR_URL}}|$PR_URL|; s|{{HEAD_SHA}}|$HEAD|; s|{{SHORT_SHA}}|$SHORT|; \
     s|{{PR_NUMBER}}|$PR|; s|{{BASE_BRANCH}}|$BASE|; s|{{PACKET_PATH}}|$PACKET|; \
     s|{{REPO_BASENAME}}|$REPO|" \
  .claude/skills/code-review/prompt.md > /tmp/cr-prompt-$$.txt

codex exec -s danger-full-access -C "$PWD" "$(cat /tmp/cr-prompt-$$.txt)"
```

The prompt instructs codex to:
- Read the packet for recon (no shell turns to rediscover PR metadata)
- Pull the diff with `git diff origin/<base>...<head>`
- Score six axes per `.agents/code-review.md`
- Run `.claude/skills/security-scan` in diff mode for the Security axis floor
- Emit FRESH scan findings in a fenced ` ```findings-new ` JSON block at the
  end of the artifact body (NOT the merged state — the helper handles that)
- NOT compute verdict / merge_ready — the helper does it deterministically

**Run codex in the background.** Reviews take ~3-4 minutes. Block only when
the next step strictly depends on the verdict. Per global guidance, fire
codex with `run_in_background: true`, surface the task id, and go do other
useful work. When the completion notification arrives, run stage 3.

**Stage 3 — finalize (deterministic):**

```bash
REVIEW_ROOT=$([ -d .reviews ] && [ ! -f .TerMinal/template.json ] && echo .reviews || echo .TerMinal/reviews)

# Extract the ```findings-new ... ``` block codex emitted in the artifact body
awk '/```findings-new/{f=1;next} /```/{f=0} f' "$REVIEW_ROOT/$PR/$SHORT.md" > /tmp/findings-new-$$.json

# Merge with prior findings.json — handles ids, first_seen_sha, auto-resolved
STATS=$(./.claude/bin/findings-merge "$PR" "$HEAD" /tmp/findings-new-$$.json)

# Extract scorecard from artifact frontmatter
SCORECARD=$(yq -r '{correctness, security, architecture, conformance, quality, dependencies}' \
  "$REVIEW_ROOT/$PR/$SHORT.md" > /tmp/scorecard-$$.json)

# Compute verdict deterministically
TEST_STATUS=$(jq -r '.test_result.status' "$PACKET")
VERDICT=$(./.claude/bin/compute-verdict "$PR" /tmp/scorecard-$$.json --test-status "$TEST_STATUS")

# Patch verdict + merge_ready back into the artifact frontmatter
VERDICT_VAL=$(echo "$VERDICT" | jq -r '.verdict')
MERGE_READY=$(echo "$VERDICT" | jq -r '.merge_ready')
RISK_TIER=$(echo "$VERDICT" | jq -r '.risk_tier')
sed -i '' "s/^verdict:.*/verdict: $VERDICT_VAL/; s/^merge_ready:.*/merge_ready: $MERGE_READY/; \
           s/^risk_tier:.*/risk_tier: $RISK_TIER/" "$REVIEW_ROOT/$PR/$SHORT.md"
```

The two helper scripts eliminate the YAML-quoting class of bugs and the
verdict-drift problem (scoring on the LLM side; verdict computed
deterministically from rules in the contract).

## Stage 4 — digest, ONLY on a passing review

The digest is the human read surface for a **mergeable** PR. Do not waste a
codex pass digesting a failing review — gate it on the verdict. Run this only
after Stage 3, only when the review passed (`approve` + `merge_ready`), and only
if the repo has the `/digest` tooling:

```bash
if [ "$VERDICT_VAL" = "approve" ] && [ "$MERGE_READY" = "true" ] && [ -x .claude/bin/chunk-diff ]; then
  git diff "origin/$BASE...$HEAD" > "$REVIEW_ROOT/$PR/$SHORT.diff.patch"
  .claude/bin/chunk-diff --patch "$REVIEW_ROOT/$PR/$SHORT.diff.patch" \
    --pr "$REPO#$PR" --short "$SHORT" \
    --out "$REVIEW_ROOT/$PR/$SHORT.chunks.json" \
    --scoped-out "$REVIEW_ROOT/$PR/$SHORT.scoped.diff" \
    $( [ -f "$REVIEW_ROOT/$PR/findings.json" ] && echo --findings "$REVIEW_ROOT/$PR/findings.json" )

  sed "s|{{PR}}|$PR|; s|{{SHORT}}|$SHORT|; s|{{BASE}}|$BASE|; s|{{HEAD}}|$HEAD|; \
       s|{{DIR}}|$REVIEW_ROOT/$PR|; s|{{DIFF_PATH}}|$REVIEW_ROOT/$PR/$SHORT.scoped.diff|" \
    .claude/skills/digest/prompt.md > /tmp/cr-digest-$$.txt
  codex exec -s workspace-write -c model_reasoning_effort="low" -C "$PWD" \
    "$(cat /tmp/cr-digest-$$.txt)" < /dev/null

  [ -f "$REVIEW_ROOT/$PR/$SHORT.digest-patch.json" ] && .claude/bin/merge-digest \
    --chunks "$REVIEW_ROOT/$PR/$SHORT.chunks.json" \
    --patch  "$REVIEW_ROOT/$PR/$SHORT.digest-patch.json" || true
fi
```

This is a separate ~60s codex pass (the `/digest` skill's), fired only for green
reviews. A `request-changes`/`blocked` review produces no digest — fix the
review first, re-review, and the digest lands when it passes.

## Token economy (vs the prior single-codex-call flow)

| Path | Cost | Wall-clock |
|---|---|---|
| Cached SHA (already reviewed) | $0 + 5s | 5s |
| Diff-hash match (rebase / amend) | $0 + 10s | 10s |
| Tests red (blocked) | $0 + 60-90s | ~tests |
| Full review (no cache) | -20-30% codex tokens vs prior | -30-60s |

## Hard rules

1. **Tests must pass.** `test_status != pass` → `verdict: blocked`. The
   preflight enforces this; codex is not invoked on red.
2. **Approve is earned.** Requires tests pass + zero ≥ medium findings.
   `compute-verdict` enforces; LLM can suggest a score but can't override
   the rule.
3. **No merge bypassing.** Push/force-push/merge to a protected branch in
   the diff → automatic critical finding → blocked. Per global §8.

## Stacked-MR batch mode

`/stacked-mr` invokes this skill **once per PR as a parallel batch** at the
end of the stack — each review still single-PR, each in its own worktree.
Preflight runs in parallel per PR; codex calls run in parallel; helpers
run after each codex returns. The diff-hash cache is shared via
`.TerMinal/reviews/.cache/` (or legacy `.reviews/.cache/`) so re-running stale
stacks is near-free.

## Fallback

If `codex` is unavailable AND OpenRouter / claude -p are configured, the
codex stage can be replaced by `cheapCall({model: 'claude-sonnet-4-6'})`
with the same prompt. Inline-Claude review loses some depth — use only
when codex is truly missing.

## Activity

After the artifact is written:

```bash
terminal-cli activity pr-verdict "Review · $VERDICT · !$PR" "$REPO @ $SHORT"
```

(The kind is auto-inferred from the title — `pr-verdict` is the explicit
fallback for clarity.)

---
name: digest
description: "Human-review digest for a PR/MR — chunks the diff into a noise-filtered, risk-ranked, code-first review surface (green/red diffs, 🟢/🟡/🔴, design-decision callouts) WITHOUT abstracting the code away. Deterministic chunker + one bounded codex pass. Sits alongside /code-review (the gate) and /iterate (tests). Use on /digest or 'digest this PR for review'. Renders in TerMinal."
---

# /digest — human-review digest (noise-filtered, code-first)

`/code-review` is the automated merge **gate**; `/digest` is the **human** read
surface. It does not decide merge-readiness. It makes a technical reviewer read
a diff in seconds by ranking it, never by hiding code — every line stays
reachable; the digest only decides what's expanded vs. collapsed and where your
eyes land. The contract + schema live in
[`.agents/digest.md`](../../../.agents/digest.md).

Output: one **in-repo** artifact `.TerMinal/reviews/<pr>/<short>.chunks.json`
(v2) or `.reviews/<pr>/<short>.chunks.json` (legacy v1), next to the review `.md`.

## Token contract (non-negotiable)

The expensive part is deterministic. The codex pass sees **only** the non-🟢
hunks and emits a **length-capped JSON patch** — never prose. 🟢 chunks
(lockfile/docs/rename/whitespace/import/generated) are classified and labeled by
`chunk-diff` and **never reach the model**. See `.agents/digest.md` → "Token
contract".

## Workflow (chunk-diff → one codex pass → merge-digest)

```
1. chunk-diff  (deterministic, $0)  → <short>.chunks.json skeleton
2. codex exec  (bounded)            → <short>.digest-patch.json
3. merge-digest (deterministic, $0) → <short>.chunks.json final
```

**Stage 0 — resolve context** (reuse the review's, or compute):

```bash
PR=<number>; HEAD=$(git rev-parse HEAD); SHORT=${HEAD:0:7}
BASE=<target branch>            # glab/gh: the MR target; default main
REPO=$(basename "$PWD")
REVIEW_ROOT=$([ -d .reviews ] && [ ! -f .TerMinal/template.json ] && echo .reviews || echo .TerMinal/reviews)
DIR="$REVIEW_ROOT/$PR"; mkdir -p "$DIR"
```

**Stage 1 — deterministic chunking** (zero tokens):

```bash
# Write the FULL diff in-repo (TerMinal renders every chunk's lines from it),
# and a SCOPED diff (non-🟢 blocks only) that is what the model actually reads.
git diff "origin/$BASE...$HEAD" > "$DIR/$SHORT.diff.patch"

.claude/bin/chunk-diff \
  --patch "$DIR/$SHORT.diff.patch" \
  $( [ -f "$DIR/findings.json" ] && echo --findings "$DIR/findings.json" ) \
  --pr "$REPO#$PR" --short "$SHORT" \
  --out "$DIR/$SHORT.chunks.json" \
  --scoped-out "$DIR/$SHORT.scoped.diff"
```

`--scoped-out` drops every 🟢 block (lockfile / generated / docs / whitespace /
rename) from the model's input. On a logic-heavy MR this is ~0% (nothing to cut);
on an MR that regenerates a big lockfile or snapshot it can be most of the diff —
the silent token killer never reaches the LLM.

For a **joint MR** (factory/stacked-MR), add `--joint "<member-mr-csv>"`.

**Stage 2 — codex digest pass** (the only LLM step; bounded output, ~60s):

```bash
sed "s|{{PR}}|$PR|; s|{{SHORT}}|$SHORT|; s|{{BASE}}|$BASE|; s|{{HEAD}}|$HEAD|; \
     s|{{DIR}}|$DIR|; s|{{DIFF_PATH}}|$DIR/$SHORT.scoped.diff|" \
  .claude/skills/digest/prompt.md > /tmp/digest-prompt-$$.txt

codex exec -s workspace-write -c model_reasoning_effort="low" -C "$PWD" \
  "$(cat /tmp/digest-prompt-$$.txt)" < /dev/null
```

Three deliberate speed/safety levers (a digest is bounded extraction, not a
review — it doesn't need depth or broad access):
- `< /dev/null` — **mandatory.** Without it `codex exec` blocks on
  `Reading additional input from stdin…` and hangs indefinitely in
  headless/background invocations.
- `-c model_reasoning_effort="low"` — the task is fill-a-JSON-patch; low
  reasoning cuts wall-clock to ~60s with no quality loss.
- `-s workspace-write` (not `danger-full-access`) — the pass only reads
  `$DIR/$SHORT.{chunks.json,diff.patch}` and writes one file under cwd. No
  tests run, so the macOS Mach-port carve-out `/code-review` needs doesn't apply.

The pass writes `$DIR/$SHORT.digest-patch.json` and nothing else.

**In factory/stacked-MR mode, skip Stage 2's separate codex call** — the joint
MR's `/code-review` codex session emits the patch alongside its review (it
already holds the diff + deterministic evidence). Only Stages 1 and 3 run
around it.

**Stage 3 — merge** (deterministic; enforces risk-override rules, sorts
decisions, recomputes stats):

```bash
.claude/bin/merge-digest \
  --chunks "$DIR/$SHORT.chunks.json" \
  --patch  "$DIR/$SHORT.digest-patch.json"
```

## Hard rules

1. **Never abstract code away.** The digest ranks and annotates; it never
   replaces the diff. TerMinal renders the real green/red lines from
   `git diff`; `chunks.json` only carries paths + hunk headers + annotations.
2. **🟢 chunks cost zero tokens.** If the codex pass writes a summary for a 🟢
   chunk, drop it in merge — green labels are deterministic.
3. **The model can't lower a deterministic 🔴.** `merge-digest` enforces this.
4. **Bounded output only.** `summary ≤ 200 chars`, one-line notes, no prose
   blocks. Diagram is emitted only for structural changes.

## Relationship to /code-review and /iterate

- `/code-review` → six-axis gate + verdict + findings (`<short>.md`). The merge bar.
- `/iterate` → tests-only refresh.
- `/digest` → the human read surface (`<short>.chunks.json`). Informational.

Run `/digest` after `/code-review` when you're about to merge a non-trivial PR
or batch, or standalone any time you want to read a diff fast.

## Activity

```bash
terminal-cli activity digest "Digest · !$PR · ${RED}🔴 ${YEL}🟡 ${GRN}🟢" "$REPO @ $SHORT"
```

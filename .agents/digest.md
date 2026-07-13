# Digest agent (`/digest`)

The **human-review digest**. Sits alongside `/code-review` (the automated
six-axis merge gate) and `/iterate` (the test-only refresh). Its job is the
opposite of the review verdict: it does **not** decide merge-readiness, it makes
a technical human review the diff in seconds instead of minutes by **filtering
noise, not abstracting code**. Every line is still reachable — the digest only
decides what's expanded vs. collapsed and where the human's eyes should land.

The reader is a technical developer who wants the ins and outs of the code.
Never summarize code away. Surface it, ranked.

Artifacts are **in-repo**, next to the review: `.TerMinal/reviews/<pr>/<short>.chunks.json`
(v2) or `.reviews/<pr>/<short>.chunks.json` (legacy v1). TerMinal renders them.

## Token contract (the whole point)

The digest is cheap because the expensive part is **deterministic**. The LLM
only ever sees the chunks a parser couldn't resolve, and emits a length-capped
JSON patch — never prose, never a re-narration of the diff.

```
.claude/bin/chunk-diff   (deterministic, 0 tokens)  → <short>.chunks.json  (skeleton: every chunk classified; 🟢 fully labeled)
codex digest pass        (bounded tokens)            → <short>.digest-patch.json (deltas for non-🟢 chunks + MR-level fields only)
.claude/bin/merge-digest (deterministic, 0 tokens)  → <short>.chunks.json  (final: skeleton + patch merged)
```

Hard rules:
- **🟢 chunks never reach the model.** Their label is set deterministically by
  `chunk-diff` (lockfile bump, docs, rename, whitespace-only, import-reorder,
  generated). Zero LLM tokens, and the model cannot re-classify them.
- **The model input is only the non-🟢 hunks**, compact, plus the skeleton's
  chunk ids — not the whole diff.
- **The model output is bounded**: per chunk `summary ≤ 200 chars`, a one-line
  `note`, optional risk override, optional `confidence` flag. Plus one MR-level
  object. No free prose anywhere.
- **Reuse, don't re-run.** In factory/stacked-MR mode the Codex `/code-review`
  session emits the digest patch as an additional output — it already has the
  diff, deterministic evidence, and findings in context, so the digest costs no
  extra model run. Standalone `/digest` runs its own bounded codex pass.
- When a `<short>.md` review artifact already exists, open findings at
  `file:line` deterministically pin that chunk to 🔴 before the model runs.

## Chunk model & deterministic classification

A **chunk = one changed file** (with its hunks). The chunks tile the **entire**
diff — nothing is silently unreviewed; the mechanical 90% is just collapsed.
`chunk-diff` assigns `kind` + default `risk` with zero tokens:

| kind | detection | default risk | green label |
|---|---|---|---|
| `lockfile` | `bun.lock`, `package-lock.json`, `Cargo.lock`, `go.sum`, … | 🟢 | `lockfile bump` |
| `generated` | `dist/`, `build/`, `*.gen.*`, `*.pb.go`, `*.snap`, `*.min.*` | 🟢 | `generated` |
| `docs` | `*.md`, `*.mdx`, `*.rst`, `*.txt` | 🟢 | `docs` |
| `rename` | git `R100` (no content delta) | 🟢 | `rename a → b` |
| `format` | every changed line whitespace-only **or** import/use-only | 🟢 | `whitespace-only` / `import reorder` |
| `test` | `**/test/`, `*.test.*`, `*.spec.*`, `e2e/` | 🟡 | — |
| `config` | `*.yml`, `*.json`, `*.toml`, `*.env*` (non-lockfile) | 🟡 | — |
| `logic` | everything else (source) | 🟡 → 🔴 | — |

**Risk floor (cannot be lowered by the LLM):** `logic`/`config` files matching a
sensitive surface (auth/session, authz/admin, payments, db/persistence/migration,
api/route, file-upload, crypto) → 🔴; any file with an open finding → 🔴. The LLM
may **raise** 🟡→🔴 (with a reason) or **lower** 🟡→🟢 only for trivial
`test`/`config` chunks; it may never lower a deterministic 🔴.

## Architecture & design decisions (first-class)

A separate, top-of-digest category — **not** a per-chunk note. A one-line diff
can bake in a decision that is expensive to reverse (a DB column, a doc/data
type, a service choice, an API contract). A technical lead must personally sign
off on these, so they are surfaced as their own list above the chunks.

`chunk-diff` flags **candidates** deterministically (`decision_signals` per
chunk: `data-model`, `api-contract`, `service-selection`, `system-design`,
`security-model`). The LLM **synthesizes** the decision list: it reads the
flagged chunks, merges signals that belong to one decision (a new type + its
route + its dep = one "added X service"), writes `what`/`why`/`alternatives`/
`reversibility`, and prunes false positives. It may add a decision the
heuristics missed. Reuses the same bounded pass — no extra run.

```json
{
  "id": "<8-char>",
  "title": "Persist consecutive-wrong streak as a dedicated column",
  "category": "data-model | system-design | service-selection | api-contract | abstraction | security-model",
  "files": ["src/db/schema.ts:50", "src/db/repo.ts:174"],
  "what": "the decision made", "why": "rationale or null",
  "alternatives": "what else could have been chosen, or null",
  "reversibility": "low | medium | high"
}
```

`reversibility: low` (migrations, public API, service lock-in) sorts first — the
sign-offs that matter most.

## LLM patch (`<short>.digest-patch.json`)

```json
{
  "brief": "3–5 sentences: what this change does, why, the blast radius.",
  "blast_radius": "one line: subsystems touched + worst-case if wrong",
  "diagram": "mermaid source, or null",
  "double_check": [{ "file": "path:line", "why": "one line" }],
  "decisions": [ /* decision objects, synthesized from decision_candidates */ ],
  "chunks": {
    "<chunk_id>": {
      "summary": "≤200 chars, what this file's change does",
      "note": "rubber-stamp | eyeball <what> | verify: <how>",
      "risk": "green | yellow | red",                  // optional override
      "confidence": "low: <why the model is unsure>"   // optional; omit when confident
    }
  }
}
```

**`diagram` is gated** — emit mermaid **only** when the change alters structure
(adds/removes files, exports, routes, cross-module edges). A pure in-function
edit gets `null` (the default), saving the tokens. Prefer `graph LR` for "how it
fits", `sequenceDiagram` for a changed flow.

## Final artifact (`<short>.chunks.json`)

Carries **no diff text** — only file paths + hunk headers. The viewer maps
`file` → the parsed diff and renders the real green/red lines from there. Header
+ `decisions` (sorted by reversibility, low first) + `decision_candidates` +
`stats` (`llm_chunks` = chunks that cost tokens, the token story) + `chunks[]`
(each: `id`, `file`, `kind`, `risk`, `status`, `added`/`deleted`, `green_label`,
`summary`, `note`, `confidence`, `decision_signals`, `hunks[]`). The viewer
renders `decisions` pinned above everything; chunks sorted 🔴→🟡→🟢; 🟢 collapsed
to `green_label`, expandable to the full diff; 🟡/🔴 expanded with chips + diff.

## Joint digest (factory / stacked-MR batches)

When a stack is built `main ← MR1 ← … ← MRn`, the stack tip already contains the
union of the batch. The factory opens one **joint MR** (`base=main,
source=<stack tip>`, draft, `do-not-merge`) whose diff is the whole batch, then
runs `/code-review` + `/digest` on it. The human reviews the batch **once**
through this combined digest instead of opening N MRs.

- `chunk-diff --joint <member-csv>` marks the artifact `"joint": { member_mrs }`.
- The per-member MRs still get their own automated `/code-review` (the merge gate
  is unchanged — each needs approve + tests + 0 medium+).
- The joint digest is **not** a merge gate; it's the human's read surface.
- The joint MR is draft + `do-not-merge`; close it when the stack lands (a future
  `merge-sync` extension can automate this).

## Invocations

```
/digest <PR-or-MR>       # standalone: chunk-diff → codex digest pass → merge-digest
```

In factory/stacked-MR mode the digest patch is emitted by the same Codex
`/code-review` session that reviewed the joint MR; `merge-digest` then assembles
`chunks.json`. See `.agents/code-review.md` → "Joint MR digest" and the
`/stacked-mr` skill.

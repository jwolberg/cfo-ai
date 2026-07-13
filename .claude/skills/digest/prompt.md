You are producing a **human-review digest patch** for MR !{{PR}} at {{SHORT}}.
Follow `.agents/digest.md` exactly. This is NOT a code review — do not score, do
not decide merge-readiness. Your only job: help a technical developer read this
diff fast, without hiding any code.

## Inputs (already on disk — read them, don't recompute)

- Chunk skeleton: `{{DIR}}/{{SHORT}}.chunks.json`
  Every changed file is already classified into a chunk with `id`, `file`,
  `kind`, `risk`, and `decision_signals`. 🟢 chunks are DONE — ignore them.
- Scoped diff: `{{DIFF_PATH}}` — contains ONLY the non-🟢 file blocks (the ones
  needing your attention). 🟢 files (lockfile / generated / docs / whitespace /
  rename) are intentionally ABSENT to save tokens; the skeleton still lists them
  so you know they exist, but do not look for or annotate their content.

## Output (write this file and nothing else)

Write `{{DIR}}/{{SHORT}}.digest-patch.json` — a single JSON object:

```json
{
  "brief": "3–5 sentences: what this MR does, why, blast radius. Prose, plain.",
  "blast_radius": "one line: subsystems touched + worst-case if wrong",
  "diagrams": [],
  "double_check": [{ "file": "path:line", "why": "one line" }],
  "decisions": [],
  "chunks": {}
}
```

## Rules (token discipline is the point)

1. **Only annotate non-🟢 chunks.** For each chunk in the skeleton whose `risk`
   is `yellow` or `red`, add an entry under `chunks` keyed by its `id`:
   - `summary`: ≤ 200 chars, what THIS file's change does. No restating the diff
     line by line.
   - `note`: exactly one of `rubber-stamp` / `eyeball <what>` / `verify: <how>`.
   - `risk`: include ONLY to override — raise 🟡→🔴 with reason in the note, or
     lower a trivial 🟡 test/config to 🟢. You may never lower a 🔴. Omit
     otherwise.
   - `confidence`: include ONLY when you are genuinely unsure
     (`"low: <why>"`). Omit when confident — silence is the signal.
   Do NOT write entries for 🟢 chunks. Do NOT echo the diff.

2. **Architecture & design decisions (first-class).** Read every chunk whose
   `decision_signals` is non-empty. Synthesize the actual decisions into
   `decisions[]`: merge signals that belong to one decision (e.g. a new type +
   its route + its dependency = one decision), write `what` / `why` (from the
   diff/comments, else null) / `alternatives` (or null) / `reversibility`
   (`low`/`medium`/`high`). Prune false positives. Add any real decision the
   heuristics missed. `low` reversibility = migrations, public API, service
   lock-in.

3. **`diagrams`** — an array of `{ "title", "kind", "mermaid" }`, **0 to a few**.
   Emit one per distinct view that genuinely clarifies the change; emit NONE for
   a trivial in-function edit (don't force it). Pick the right kind per view:
   - `flow` — `graph LR`/`graph TD` for how-it-fits / module wiring
   - `sequence` — `sequenceDiagram` for a changed request/call flow
   - `er` — `erDiagram` for data-model / schema / table changes
   - `state` — `stateDiagram-v2` for a state machine / status lifecycle
   `title` is a short label; `mermaid` is valid mermaid source for that kind. A
   schema + new flow might warrant two diagrams (an `er` and a `sequence`).

4. **`double_check`** — the 1–3 specific spots a human should personally eyeball,
   by `file:line`. Empty array if it's all rubber-stamp.

5. **No prose anywhere but `brief`.** No markdown, no commentary, no review
   verdict. Just the JSON object, written to the path above.

When done, confirm only: the patch path + counts (chunks annotated, decisions).

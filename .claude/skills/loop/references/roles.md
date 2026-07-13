# Roles and handoffs

Three roles, three context windows, three system prompts. The full prompt for
each lives in its own skill so it can be assigned to a dedicated session:
`/loop-planner`, `/loop-generator`, `/loop-evaluator`. This file is the
**handoff contract** between them — who reads what, who writes what, and the two
handshakes that matter.

## Who owns what

| File | Planner | Generator | Evaluator |
|---|---|---|---|
| `contract.md` | drafts | proposes edits | pushes back, marks pass/fail |
| `feature_list.json` | — | updates status | updates evidence |
| `progress.md` | seeds | updates phase/next | updates bottleneck |
| `log.md` (append) | init | `generate#N` | `evaluate#N` |
| `scores/NNNN.md` | — | — | writes |
| code / worktree | never | writes | reads only |

## Handshake 1 — contract negotiation (before any code)

1. Planner turns the human sentence into a spec and drafts `contract.md`
   assertions (10–30, each testable).
2. Evaluator reviews the draft *adversarially*: which assertions are vague,
   un-testable, or rubber-stampable? It rewrites them into checkable form and
   pushes back via `events.jsonl` (`kind: request`).
3. They iterate until the evaluator signs off (`kind: status`, "contract
   agreed, N assertions"). Only then does the generator start.
4. If taste matters, the planner + evaluator agree the `## Taste calibration`
   references (3 good / 3 slop) in the same pass.

The planner's spec is the **boundary**; the agreed contract is what gets
**graded**. Do not let the generator negotiate its own grading.

## Handshake 2 — build / review (each iteration)

1. Generator implements the next unmet assertions, updates `feature_list.json`
   + `progress.md`, appends `log.md`, emits `ready-for-review`.
2. Evaluator reads the **diff and traces** (not the generator's summary of
   itself), runs the app/tests, marks each touched assertion pass/fail with
   bounded evidence, and — for taste work — writes `scores/NNNN.md`.
3. Evaluator emits `complete` (all pass, plateaued) or `request` (what's still
   failing + the exact next action). The operator (`/loop`) decides
   continue / restart / stop.

## Rules that bind all three

- **Bounded context.** 80 lines / 12k chars default; pointers over payloads.
- **No role bleed.** The generator never grades; the evaluator never quietly
  fixes code; the planner never writes code. A role that does another's job is
  the failure mode this whole structure exists to prevent.
- **Keep listening.** In live-paired mode, no role stops listening until the
  user stops the loop (see `loop-supervisor/references/protocol.md`).
- **Escalate the contract, not the build.** A failing iteration is normal; a
  wrong contract is a human gate.

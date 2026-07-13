# Loop state — on disk, never in context

Everything a loop needs to survive a crash lives in one directory. The test:
**the model can lose its session and resume from three files.** If it can't,
the state is too complicated.

## Layout

```
.TerMinal/loops/<loop-id>/
  contract.md         # the agreed, gradable definition of done  (the thing that gets graded)
  feature_list.json   # structured status of each contract assertion
  progress.md         # human-readable: current phase, bottleneck, next step
  log.md              # APPEND-ONLY event journal
  events.jsonl        # role-to-role channel (see loop-supervisor/protocol.md)
  scores/NNNN.md      # one taste score per evaluated iteration
```

The generator works in an isolated worktree so a restart is a clean delete:
`../.worktrees/<repo>/loop-<loop-id>/`. State above lives in the **main** repo
so it survives the worktree being nuked.

## The three resume files

On startup any role reads, in order: `contract.md`, `feature_list.json`,
`progress.md`. That is enough to know the goal, what's done, and what's next.

### contract.md

The negotiated checklist of testable assertions (rule III). Boundary is the
planner's spec; the contract is what the evaluator grades. Example:

```markdown
# Contract: <loop-id>
Goal: <one sentence from the planner>
Boundary: <what is explicitly out of scope>

## Assertions
- [ ] A1  Landing page renders at / with no console errors
- [ ] A2  "Get started" opens the signup modal within 200ms
- [ ] A3  Signup rejects invalid emails with an inline error
- ...    (aim for 10–30; fewer than ten gets rubber-stamped)
```

Assertions must be **testable** — a command, a Playwright step, or an evaluator
check can decide pass/fail. "Looks good" is not an assertion.

### feature_list.json

```json
{
  "loopId": "<loop-id>",
  "iteration": 7,
  "assertions": [
    { "id": "A1", "status": "pass", "evidence": "curl / -> 200, 0 console errors", "checkedIter": 7 },
    { "id": "A2", "status": "fail", "evidence": "modal opens at ~600ms", "checkedIter": 7 },
    { "id": "A3", "status": "todo" }
  ]
}
```

`status` ∈ `todo | in-progress | pass | fail`. `evidence` is a bounded string
(command output tail, a file:line, a test name) — never a pasted log.

### progress.md

```markdown
# Progress: <loop-id>
Iteration: 7
Phase: generate            # negotiate | generate | evaluate | score | decide
Bottleneck: verification   # coding | planning | verification | taste  (rule IX)
Last taste score: 0.71
Next: fix A2 modal timing, then re-run the evaluator on A2–A3
```

### log.md (append-only)

One line per operation. Never rewrite prior entries.

```markdown
## [2026-07-02] init | goal: onboarding polish
## [2026-07-02] contract | 14 assertions agreed
## [2026-07-02] generate#7 | modal + inline validation
## [2026-07-02] evaluate#7 | 11/14 pass, A2/A8/A11 fail
## [2026-07-02] restart | run drifted into archaeology; reset worktree, kept contract
```

## events.jsonl

The role-to-role channel. Reuse the existing message shape from
`loop-supervisor/references/protocol.md` (JSONL: `loopId, role, kind, sessionId,
summary, detail, createdAt`). Bounded `detail` only; pointers over payloads.

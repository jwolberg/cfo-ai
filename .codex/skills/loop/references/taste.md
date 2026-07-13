# Taste rubric — score the subjective

Taste is gradable if you write it down. The model will not invent taste; it
converges toward the taste you describe. So the whole game is writing this
rubric carefully enough that converging on it is what you actually wanted.

Only the **evaluator** scores taste, and only for taste-bearing work (UI, prose,
design, DX). Pure-backend loops can skip this and grade on assertions alone.

## The four axes

Score each in `[0,1]`, then combine with weights. Default weights (tune per loop
in `contract.md`):

| Axis | Weight | Asks |
|---|---|---|
| **Design** | 0.30 | Visual/structural coherence. Hierarchy, spacing, rhythm, restraint. Does it read as one intentional system? |
| **Originality** | 0.20 | Is this a default template, or a considered choice? Would a sharp reviewer call it AI-slop? |
| **Craft** | 0.30 | Details: states (hover/empty/loading/error), edge cases, copy, micro-interactions, no rough edges. |
| **Functionality** | 0.20 | Does the intended job actually work, end to end, on the real path — not just the happy demo? |

`score = 0.30·design + 0.20·originality + 0.30·craft + 0.20·functionality`

## Calibration (do this once per loop, before scoring)

Pin the scale to concrete references so scores mean something:

1. Name **three references the evaluator is told are good** (~0.85–0.95) and
   **three it's told are slop** (~0.15–0.30). Real URLs, screenshots, or files.
2. Record them in `contract.md` under `## Taste calibration`.
3. Every score is relative to those anchors, not an absolute felt sense.

## Output shape

Write to `scores/NNNN.md` (NNNN = iteration):

```markdown
# Taste score — iteration 7
design: 0.72        # 3-col layout coherent, but the card shadows fight the border tokens
originality: 0.55   # header is close to the default shadcn dashboard; hero lacks a POV
craft: 0.68         # empty + loading states done; error toast has raw JSON; focus ring missing on the modal
functionality: 0.90 # core flow works; deep-link to /signup 404s
weighted: 0.71

Gap to 0.85: unify shadow/border tokens, give the hero an opinion, and fix the
two craft misses (error toast, focus ring). Functionality is fine; taste is the
bottleneck now.
```

The paragraph is not optional — it names the **gap**, which is what the next
generator iteration acts on. A bare number tells the loop nothing.

## Convergence

Stop scoring when the last two weighted scores are within ~0.02 **and** all
contract assertions pass. A plateau below target with assertions passing means
the *rubric* is the limit — escalate to the human (the contract, not the build,
is what's wrong).

---
name: vibe
description: "Enter vibe mode — fast, gates-off exploration in a disposable worktree to generate breadth and signal, never shipped directly. Use when the user says /vibe, 'vibe this', 'explore fast, skip the gates', 'prototype N approaches', or wants throwaway iteration to learn the shape of a problem before building it for real. NOT for production work — that's quality mode (the default)."
---

# /vibe — Enter vibe mode (with guardrails)

Vibe mode optimizes for **breadth, speed, and information**, not shippable code.
Generate real working artifacts fast, in a disposable branch, to learn the shape
of a problem — then throw them away and rebuild the good parts in quality mode.
This skill sets up that contract and arms the guardrails. See CLAUDE.md §14.

## When to use / not use

- **Use** for discovery: prototype competing approaches, test a risky
  architecture decision, generate N variants to compare, vibe an end-to-end
  artifact to mine for references. Output is disposable signal.
- **Do NOT use** for anything meant to ship. Production work is **quality mode**
  — the default — with TDD, the `code-review` agent, and a human merge (§1
  loop). If unsure which mode the work is, it's quality mode.

## The contract (state this on entry)

- Gates **off**: skip code review, skip the PR/MR, minimal HITL. The agent
  self-steers and comes back with something to show.
- Output is **disposable**: reference material and signal, not a shipping
  candidate. Not promoted to production — the exit is a quality-mode rebuild.
- The human **reads output, steers, and fails bad branches early** — not
  reviews every step.

## Guardrails (set them up, don't just assert them)

1. **Isolate first.** Create/confirm a disposable worktree before any vibe work.
   Never vibe in the primary checkout, never on `main`.

   ```bash
   repo=$(basename "$(git rev-parse --show-toplevel)"); branch=vibe/<short-slug>
   git worktree add "${WORKTREES_DIR:-$HOME/.worktrees}/$repo/$branch" -b "$branch"
   ```

   For a single-file spike, at minimum cut a `vibe/*` branch. The
   `.claude/hooks/block-main-merge.sh` hook (global §8) already makes
   merge-to-main impossible — vibe mode does not loosen that.

2. **No production side effects.** No prod infra, secrets, external services,
   shared databases, or anything with irreversible blast radius. Reversible/
   sandboxed only.

3. **Label it disposable** (branch `vibe/*`, a note to the user) — not owned
   code, won't ship as-is.

4. **Never merge** a vibe branch. It's read for signal and discarded.

## Working in vibe mode

- Move fast. Generate options, variants, competing approaches. Expect
  messiness; don't protect the first artifact.
- Techniques that fit: **galleries** (N whole variants, pick/combine),
  **studios** (N variants along tuned knobs), **observability dashboards /
  debug toggles** (build a reusable inspection surface into the app instead of
  a one-off manual check — a favorite, highest-leverage move),
  **settings-toggles / endpoint-N** (turn the debate into a switch), and
  running one ticket as **N lanes** to compare.
- Steer continuously — read output, correct drift, kill bad branches early.
  Vibe mode is fast exploration under a human's eye, not autopilot.

## Exiting to quality mode (clean handoff)

1. Ask: **"Using what you know now, how would you reimplement this from scratch
   for better quality?"**
2. Distill decisions the pass surfaced → tickets (`/ticket`).
3. Rebuild in quality mode: clean start, cherry-pick good code/tests/API-shapes/
   edge-cases from the vibe branch as *reference*, review and verify each step.
4. Discard the worktree once signal is harvested:
   `git worktree remove "${WORKTREES_DIR:-$HOME/.worktrees}/$repo/$branch" --force`

## Hard rules

1. Quality mode is the default; vibe mode is explicit and temporary.
2. Vibe work is isolated (worktree/`vibe/*` branch), never `main`, never the
   primary checkout.
3. No production side effects.
4. Vibe artifacts never ship directly — the exit is always a quality-mode
   rebuild.
5. Never merge a vibe branch.

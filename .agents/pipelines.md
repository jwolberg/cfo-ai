# pipelines (design proposal — not yet implemented)

A pipeline is an **ordered sequence of steps** that can mix deterministic shell
scripts with LLM calls. The goal is to stop burning tokens on every cron
firing when 90 % of runs just need a cheap pre-check. A pipeline runs in the
same launchd / Schedules tab slot as an agent today; the runner picks the
pipeline based on whether the schedule references a `pipelineId` instead of
an `agentId`.

This file is the design — the runtime change ships in a follow-up.

## Why

Today every schedule fire can become one full LLM run. For the health agent,
that means spending model context every few minutes just to find "everything's
green." The right shape is:

1. **Run a deterministic check** (a shell script). If it exits 0, we're done
   — no tokens spent.
2. **If it fails** (non-zero exit), escalate to claude or codex with the
   failure context, and let the LLM diagnose + propose a fix / file HITL.

A pipeline encodes this conditional escalation so it doesn't need to be
hand-coded into every agent prompt.

## Step shape

```yaml
# .agents/pipelines/health-then-llm.yml
name: Health check with conditional LLM escalation
description: Cheap deterministic check first, escalate to claude only on failure.
steps:
  - id: precheck
    type: script
    run: |
      #!/bin/bash
      set -euo pipefail
      bunx tsc --noEmit -p tsconfig.json
      bun test --bail
      bun audit --severity high
      curl -fsS http://localhost:3000/healthz

  - id: diagnose
    type: llm
    when: previous_failed
    engine: claude
    model: haiku                # cheap by default; spec can override
    prompt: |
      The precheck failed:
      ${prev.exit_code} from `${prev.step_id}`
      stderr (last 200 lines):
      ${prev.stderr_tail}
      Diagnose root cause, propose a fix, and if it's safe + scoped, apply it
      and open a PR per the project's pr-creation conventions. Otherwise
      file a ticket + HITL.
```

## Conditions

Each step declares when it runs:

- `when: always` (default for the first step) — runs unconditionally.
- `when: previous_succeeded` — runs only if the prior step exited 0.
- `when: previous_failed` — runs only if the prior step exited non-zero.
- `when: any_previous_failed` — runs if any step before it failed.
- `when: always_after` — terminal cleanup, runs regardless (errors carry).

## Step types

- `type: script` — runs the inline `run:` block (or a `file:` path) through
  the same `script -q /dev/null` PTY wrapper the runner uses today, so output
  streams to the run log in real time. Exit code is captured; stdout/stderr
  tails are exposed to subsequent steps via `${prev.stdout_tail}` /
  `${prev.stderr_tail}`.
- `type: llm` — same prompt run as today's agent, with the additional
  `${prev.*}` interpolations available. Each LLM step inherits the
  schedule's worktree but can be marked `inPlace: true` to run in the repo
  itself (e.g. for orchestrators).

## Storage + scope

Mirrors the agents convention:

- Per-repo: `.agents/pipelines/<id>.yml`
- Global:   `~/.config/TerMinal/pipelines/<id>.yml`

Pipelines reference agents (or inline LLM prompts). When a step references
`agent: docs`, the LLM step reuses that agent's `prompt`, `engine`, and
`model` defaults — keeping pipelines small and avoiding prompt drift.

## Schedule integration

A schedule entry gains an optional `pipelineId` alongside `agentId`. The
existing `agentId` model still works for the single-step case (most
schedules will keep that). A schedule may reference one or the other, not
both. The runner branches at the start of `runSchedule`:

```
if (sched.pipelineId) runPipeline(sched.pipelineId, ctx)
else                  runSingleAgentPrompt(sched.agentId, ctx)
```

## UI surface

Two additions:

1. **A `Pipelines` view** alongside Schedules/Agents — list per-repo +
   global pipelines, show their steps as a small flow diagram (step icon,
   type chip, when-condition), and let you trigger a one-off run for testing.

2. **Step-by-step run logs** in the Schedules → Runs view: each step gets
   its own collapsible block with status badge (passed / failed / skipped /
   running), duration, and its captured stdout/stderr. Sketches naturally
   on the existing run-log expander.

## Phase plan

- **Stage A (this file).** Design + convention locked. No runtime yet.
- **Stage B.** Parser for `.yml`, basic two-step pipeline runtime
  (precheck script → LLM-on-fail), step-by-step logs in the runner output.
- **Stage C.** Pipelines tab + the inline flow-diagram preview. Step list
  in the schedule create form ("schedule this pipeline" alongside "schedule
  this agent").
- **Stage D.** Conditional matrix (any_previous_failed, always_after) +
  per-step model override + cross-step variable interpolation beyond
  `${prev.*}`.

## What this is not

- **Not a general-purpose workflow engine** (Argo / Temporal / Prefect).
  These are background-job orchestrators with retries, durable state,
  parallel branching, and worker pools. Pipelines here are *small*: 2–5
  steps, single-machine, single-process, optimized for the
  "cheap-then-expensive" pattern.
- **Not a replacement for a CI matrix.** CI gates code at merge time;
  pipelines are scheduled background work that may *use* a CI-style script
  step but don't pretend to replace one.
- **Not a substitute for the `/check` framework.** `/check <kind>` is the
  single-agent scheduled inspection; pipelines compose check kinds with
  conditional script gates. A check kind can be invoked from a pipeline
  step as `type: llm, agent: <check-kind>` once Stage B lands.

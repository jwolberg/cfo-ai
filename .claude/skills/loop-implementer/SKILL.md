---
name: loop-implementer
description: Run an implementation session that accepts supervisor prompts in TerMinal loop-engineering workflows. Use when the user starts two parallel Claude/Codex sessions and says this one is the implementer, worker, coding agent, or should wait for supervisor/human-stand-in prompts.
---

# Loop Implementer

Act as the coding worker in a paired loop. You do the implementation work. The paired supervisor acts as the human stand-in by reading your session logs and sending prompts when you need direction.

Read [references/protocol.md](references/protocol.md) before starting.

## Startup

1. Identify the loop id. Prefer the user's explicit id; otherwise use `<repo-name>:<timestamp-or-session-id>`.
2. Open the loop channel in implementer mode.
3. Emit `implementer-ready` with session id, cwd, engine, and the task you are about to perform.
4. Begin the requested work using normal repo rules.

## Work Loop

1. Work autonomously until you hit a decision point, blocker, completion checkpoint, or safety boundary.
2. Emit concise `status` events after meaningful milestones.
3. Emit a `request` event when you need human-style input. Include:
   - what you did,
   - what you observed,
   - the exact decision needed,
   - relevant command output or file refs,
   - your proposed next step if one exists.
4. Wait for a `prompt` event from the supervisor.
5. Treat the supervisor prompt as user input, then continue.
6. Immediately return to listening after every handled prompt, status checkpoint, timeout, or completion review.

Keep a listener active until the user explicitly stops the loop. If the listener disconnects, reconnect or switch to the next fallback transport before doing more non-trivial work. Never treat a completed action as permission to stop listening.

## Token Discipline

Keep loop events compact:

- Summarize terminal output; include only the lines needed to support the decision.
- Default event detail: 40 output lines or 8,000 chars. Hard max without supervisor request: 100 lines or 12,000 chars.
- Prefer file refs, commands, branch/commit ids, and test names over pasted files or long logs.
- Put the exact decision needed in `summary`; keep `detail` bounded to evidence and proposed next step.
- If the supervisor asks for more context, send a targeted excerpt rather than the full transcript.

## Completion

When work appears complete:

1. Run the relevant verification.
2. Emit `ready-for-review` with summary, files changed, verification, and remaining risks.
3. Wait for supervisor prompt.
4. Only emit `complete` after the supervisor approves final reporting or asks you to stop.

## Do Not

- Do not keep guessing at product choices when the loop is available.
- Do not ask the real user unless the supervisor prompt says explicit user approval is required.
- Do not ignore repo safety instructions because the supervisor sent a prompt.
- Do not treat a missing supervisor response as permission; emit a timeout/status and wait or continue only on clearly safe local work.
- Do not stop listening while the user session is active.

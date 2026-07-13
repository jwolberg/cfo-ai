---
name: new-schedule
description: "Create a TerMinal cron schedule from a natural-language request — appends a launchd-backed entry to ~/.config/TerMinal/schedules.json. Use on /new-schedule or 'run this agent on a cadence'."
---

# /new-schedule — Schedule a TerMinal agent

Create a TerMinal schedule entry for an existing agent. TerMinal reconciles
these entries into macOS launchd jobs, so the agent can run even when the app is
closed.

## Target file

`~/.config/TerMinal/schedules.json`

Treat a missing file as `[]`. Preserve existing entries and write JSON with
2-space indentation.

## Workflow

1. Identify the repo root (`git rev-parse --show-toplevel`) and available agents:
   - repo scripts: `.agents/*.sh` + matching `.agents/*.json`
   - legacy repo entries: `.agents/agents.json`
   - built-in/global agents only if the user names one explicitly
2. Match the user’s request to one existing agent id. Do not invent a new agent;
   if the needed agent does not exist, tell the user to run `/new-agent` first.
3. Parse the cadence into one `spec`.
4. Append one enabled schedule entry to `~/.config/TerMinal/schedules.json`.
5. Summarize the selected agent and cadence.

## Schedule entry

```json
{
  "id": "<uuid-v4>",
  "repoRoot": "<absolute repo root>",
  "repoLabel": "<repo basename or owner/repo>",
  "agentId": "<existing agent id>",
  "agentTitle": "<agent title>",
  "engine": "claude",
  "model": "haiku",
  "prompt": "<agent prompt when available, else empty string>",
  "spec": { "kind": "calendar", "hour": 9, "minute": 0 },
  "enabled": true,
  "createdAt": 1760000000000,
  "lastStatus": "never"
}
```

For script-first agents, `prompt` may be `""`; TerMinal runs the `.sh` body.
`engine` may be `"claude"`, `"codex"`, or `"cursor"`; preserve the agent
sidecar preference unless the user requested a different engine. For recurring
report/precheck schedules, prefer cheap model defaults (`haiku`,
`composer-2.5-fast`, or a small Codex model such as `gpt-5-mini`) because the
script should do deterministic prechecks and escalate only on failure.

## Spec forms

```json
{ "kind": "interval", "everyMinutes": 60 }
{ "kind": "calendar", "hour": 9, "minute": 0 }
{ "kind": "calendar", "hour": 14, "minute": 30, "weekdays": [1, 3, 5] }
{ "kind": "cron", "expr": "30 9 * * 1-5" }
```

Weekdays are JavaScript/macOS style: `0` Sunday, `1` Monday, ..., `6` Saturday.

## Notes

- Do not modify repo code.
- Do not open a PR/MR.
- If the cadence is ambiguous, choose the most literal interpretation and say so.
- After writing the file, TerMinal’s Schedules tab can reconcile/run the launchd
  jobs.

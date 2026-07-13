---
name: notify
description: "On-demand AFK Telegram bridge: send completion/blocker pings and receive replies via a background listener that wakes the session. OFF by default. Use on /notify, going AFK, or steering from Telegram."
---

# /notify — On-demand AFK Telegram bridge

A two-way bridge between an AFK user and this session via the user's personal
Telegram bot. **Off by default** — arm it only when the user signals AFK ("I'm
AFK", "ping me when done", "steer from Telegram", "going for a walk").

## Prerequisite (machine-level, set up once)

The send/receive scripts and credentials live in the user's home, not the repo
(they're personal + secret, so they can't be committed):

- `~/.claude/bin/telegram-notify.sh` — **send** a message
- `~/.claude/bin/telegram-poll.sh` — **fetch** new messages (offset-tracked)
- `~/.claude/bin/tg-listener.sh` — **foreground listener loop** (receive)
- `~/.claude/.telegram-notify.json` — `bot_token` + `chat_id` (mode 600)

If these don't exist on the machine, the bridge can't run — tell the user and
stop (this template can't provision personal Telegram creds). When they exist,
this skill just operationalizes them for the current repo/session.

## Arming (when the user goes AFK)

1. **Immediately** launch the listener via `Bash(run_in_background=true)`:
   ```
   Bash(command="~/.claude/bin/tg-listener.sh", run_in_background=true)
   ```
   The loop polls Telegram and exits the moment a message arrives — that exit
   wakes the session. Double-arming is safe (it self-prunes prior instances).
2. Send a one-line ack: `~/.claude/bin/telegram-notify.sh --kind=info "On it — armed for AFK."`
3. Work autonomously. When the listener fires, ingest the message and respond.
4. **Re-arm a fresh listener at the end of every turn while AFK** — that's what
   keeps the bridge continuously connected.
5. Disarm when the user says "I'm back" / "AFK off" / "stop pinging": stop
   re-arming.

## When to send (send side is event-driven, not per-turn)

- Major checkpoints: work kickoff, a PR opened, a phase done, the whole task done.
- A true blocker needing a human decision (`--kind=blocked` / `--kind=question`),
  with the question + 2-3 options.
- `/notify <message>` — send verbatim (`--kind=info` unless a kind is passed).
- Heartbeat: if a single task runs >5 min with no ping, send a one-line
  `--kind=info` so the user knows it's alive.

Don't ping per shell command, for routine completions, or for things the user
is watching on screen.

## Message kinds

`--kind=done` (✅) · `--kind=blocked` (⛔) · `--kind=question` (❓) ·
`--kind=info` (ℹ️) · none (plain). Phone-readable: 1–4 lines, lead with the
outcome, include URLs/paths, plain text (no markdown). Never send secrets.

```bash
~/.claude/bin/telegram-notify.sh --kind=done "PR #42 opened, tests green. Review running in background."
~/.claude/bin/telegram-notify.sh --kind=blocked "Migration needs prod creds. Skip backfill and ship, or wait?"
```

## Relationship to autonomous modes

`/stacked-mr` (autonomous overnight stacking) runs AFK by default and uses this
bridge for its checkpoint pings. When the user kicks off `/stacked-mr`, arm the
listener as part of starting it.

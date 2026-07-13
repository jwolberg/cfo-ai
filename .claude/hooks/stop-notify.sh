#!/usr/bin/env bash
# Claude Stop hook: file a default-on TerMinal Inbox item when a turn completes.

input=$(cat)
cwd=$(printf '%s' "$input" | jq -r '.cwd // empty')

case "$cwd" in
  */.claude-mem/*|*/.claude-mem) exit 0 ;;
esac

if [ -x "$HOME/.config/TerMinal/bin/terminal-cli" ]; then
  printf '%s' "$input" | "$HOME/.config/TerMinal/bin/terminal-cli" completion-hitl Claude >/dev/null 2>&1 || true
fi

exit 0

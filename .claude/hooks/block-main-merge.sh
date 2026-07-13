#!/bin/bash
# PreToolUse hook: block merge/push operations targeting main or master.
# Enforces global CLAUDE.md §8 — no merges to protected branches without
# human approval. Reads tool call JSON from stdin. Exits 2 with stderr to
# deny the tool call.
#
# This is the per-project copy carried by the workflow template. Unlike the
# autopilot-harness copy, it has NO bypass path — every repo this lands in is
# a real project where merge-to-main is human-only.

set -u

input=$(cat)
tool=$(printf '%s' "$input" | jq -r '.tool_name // ""')

# Only inspect Bash invocations.
[ "$tool" = "Bash" ] || exit 0

cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // ""')
cwd=$(printf '%s' "$input" | jq -r '.cwd // ""')

block() {
  echo "BLOCKED: $1" >&2
  echo "Rule: no merges or pushes to main/master without human approval (global CLAUDE.md §8)." >&2
  echo "If the human has approved this specific action, run it in a non-Claude terminal." >&2
  exit 2
}

# 1. PR/MR merge commands — never allowed from an agent.
echo "$cmd" | grep -qE '(^|[[:space:]&|;])(gh[[:space:]]+pr[[:space:]]+merge|glab[[:space:]]+mr[[:space:]]+merge)\b' \
  && block "PR/MR merge command (gh pr merge / glab mr merge)"

# 2. Pushes that explicitly target main/master as a refspec.
#    Matches: `... main` (end-of-arg), `... master`, `:main`, `:master`, `/main`, `/master`.
echo "$cmd" | grep -qE '\bgit[[:space:]]+push\b.*([[:space:]]|:|/)(main|master)([[:space:]]|$)' \
  && block "git push targeting main/master refspec"

# 3. Pushes that include all branches (would push local main if it exists).
echo "$cmd" | grep -qE '\bgit[[:space:]]+push\b.*(--all|--mirror)\b' \
  && block "git push --all/--mirror could include main/master"

# 4. Bare `git push` (no refspec) while the cwd's current branch is main/master.
if echo "$cmd" | grep -qE '(^|[[:space:]&|;])git[[:space:]]+push([[:space:]]+(-u|--set-upstream))?[[:space:]]*($|[&|;])'; then
  if [ -n "$cwd" ] && [ -d "$cwd/.git" ]; then
    branch=$(git -C "$cwd" branch --show-current 2>/dev/null || echo "")
    case "$branch" in
      main|master) block "bare 'git push' while on $branch" ;;
    esac
  fi
fi

exit 0

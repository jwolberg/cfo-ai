Review PR {{PR_URL}} at head commit {{HEAD_SHA}}. Output the review artifact at .TerMinal/reviews/{{PR_NUMBER}}/{{SHORT_SHA}}.md, unless this is a legacy v1 repo that already has .reviews/ and no .TerMinal/template.json marker; in that case use .reviews/{{PR_NUMBER}}/{{SHORT_SHA}}.md.

A deterministic preflight has already done the recon. Read the packet at {{PACKET_PATH}} for: PR metadata, file list, language histogram, surface flags (auth/migrations/routes/deps/etc.), test results (already executed + cached), prior findings + suggestions count, diff_hash, review_kind_hint.

Pull the diff yourself with `git diff origin/{{BASE_BRANCH}}...{{HEAD_SHA}}` — this is the source of truth for what you're reviewing.

Follow .agents/code-review.md for the scoring rubric + artifact format. Score the six axes (correctness, security, architecture, conformance, quality, dependencies). For Security, run `.claude/skills/security-scan` in diff mode first; take the lower of its recommended score and your manual read.

Compose findings with copy-pasteable fix prompts in the body. After writing the artifact, emit a fenced ```findings-new ... ``` block containing the FRESH scan findings as a flat JSON array — the harness helper merges this with the prior findings.json deterministically (handles ids, first_seen_sha, auto-resolved transitions). Do NOT compute verdict or merge_ready yourself — the verdict helper will compute those from the scorecard + findings + test_status.

If real work falls out of scope (latent bugs, refactors, missing tests), first use
`terminal-cli mcp list_agents repo={{REPO_BASENAME}}` to choose exactly one owner,
then file backlog tickets via `terminal-cli mcp file_ticket repo={{REPO_BASENAME}}
title='...' type=... agentId=... agentScope=... agentKind=...` and reference the
slug in Suggestions. If a finding needs multiple agents/phases, file multiple
linked tickets instead of one broad ticket.

The shell sandbox is danger-full-access — the test gate may bind loopback ports or write /tmp.

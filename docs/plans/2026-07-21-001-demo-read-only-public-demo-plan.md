---
title: The CEO demo — the read-only public demo, told as the 0057 story
type: demo
status: draft
date: 2026-07-21
origin: CEO asked "how much of this is AI driven code?"; the demo is the answer's evidence
ticket: docs/tickets/0057-deployed-web-shows-a-linked-household.md
---

# The CEO demo — the read-only public demo, told as the 0057 story

## Summary

The demo is **not** "here is the app." It is: *I tried to deploy this and show you a real linked
household, and the interesting part is why I couldn't.* The 0057 arc — measure what production
actually needs, find that three of the four blockers are security/architecture gaps rather than deploy
chores, pick a lane on a stated safety criterion, ship it, then prove it fails closed — is the only
thing in this repo that demonstrates all four of the things the demo has to demonstrate.

## What the demo has to accomplish

1. That I can determine the **right problem set** — find the real blockers, not the obvious ones.
2. That I can **implement** that problem set.
3. That the work shows **competence**, not just output.
4. That it is something I would need to **verify operationally correct myself anyway** — so the
   preparation is real work, not demo theatre.

## The arc (~10 minutes)

### 1. The problem set (criterion 1)

The goal was to deploy and show live linked data. Measuring what production needed surfaced four
independent blockers, three of them real gaps. Lead with blocker #3:

> The backend derives role and membership from the **presented token's own claims**
> (`backend/identity/deps.py`) — there is no concept of "this is the restricted demo credential."
> Baking an owner `session_jwt` into a public web bundle would hand every anonymous visitor full read
> **and owner writes** — `PATCH /policy`, `POST /attest`, Plaid link-exchange — over real financial
> data.

That is the sentence a fintech CEO should sit up for. It was found by reading the auth path, not by
getting burned. Mention the other three briefly (5-minute `session_jwt` with no refresh built; no web
sign-in path exists; production structurally cannot hold the data).

### 2. The decision (criterion 3)

Three lanes were written down, and one was picked on an explicit criterion:

> The safety property must be **technical, not deploy-discipline** — a wrong bake or an expired token
> has to fail closed, not open.

Lane C, the demo plane. The demo session is minted server-side on demand (`backend/identity/demo.py`,
`POST /demo/session`) and can only ever mint a **viewer**, so an unauthenticated public endpoint
handing out credentials still cannot do anything but read. The competence signal is not that it
shipped — it is that the two easier lanes were rejected for a stated reason.

### 3. It is live (criterion 2)

Show the public site. Real Sandbox-derived decision, real card statement balances, the spend surface —
all served through the live loop, not a seeded fixture.

### 4. The negative test, run live (criterion 4)

Open a terminal in front of them and fire `PATCH /policy` and `POST /attest` at the **deployed** API
with the public demo session. Both must **403**.

This step is the reason to pick this demo at all. Every candidate demos a happy path; almost nobody
demos the write that is *supposed* to fail, against production, live. It is the difference between
"I built a thing" and "I know how my thing fails."

## Preparation owed before offering the demo

**Re-run the fail-closed check against the deployed API.** `docs/implementation-notes.md` records
structural read-only verified **locally** (403 on both write paths, 2026-07-18), but the notes stop at
Stage 0 — stages 1–4 were the production steps, and the fail-closed check re-run against the deployed
API is not written down anywhere.

- If it 403s: that is the closer, and the result gets appended to the implementation notes.
- If it does not: that is the single most important thing to know about this deployment, found before
  the CEO finds it.

Either outcome is worth the time independent of the demo. That is criterion 4 being satisfied honestly
rather than nominally.

## Caveat to volunteer, not defend

Say out loud, during the demo, that this is **Plaid Sandbox data, not real accounts** — and give the
20-second reason: production Plaid is structurally blocked at boot
(`assert_plaid_tokens_safe_at_rest()`) until KMS envelope encryption lands, plus a Plaid Trial plan is
required for real production data.

Volunteering it reads as rigor. Being asked and having to admit it reads as spin.

## Explicitly not in this demo

- A code tour. The offer stands ("pick a file and I'll tell you why it's shaped that way"), but the
  driven demo is the 0057 arc.
- The mobile native app, the sweep-execution rung, or anything gated behind unbuilt KMS work.
- Any claim about production Plaid data. Sandbox, stated up front, once.

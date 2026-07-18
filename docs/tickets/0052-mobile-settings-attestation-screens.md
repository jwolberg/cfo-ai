---
id: "0052"
title: Mobile — the Settings + Attestation write screens
type: feat
status: open
priority: high
repo: cfo-ai
agentId: mobile-rn-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-17-003-feat-identity-and-settings-controls-plan.md
depends_on: ["0049", "0050", "0051"]
created: 2026-07-18
---

# Mobile — the Settings + Attestation write screens

Unit **U6b**. The product's first user-facing write screens: Settings (guardrails + pause) writing
via U4, Attestation ("confirm your cards") writing via U5 — with their interaction states specified,
not left to the implementer. Keep the screens minimal; this is where scope balloons.

Requirements: the write paths (U4/U5); U6a (session + household context).

## Interaction states (specified, not hand-waved)

- **Settings states — the first write, no silent no-ops:** map U4's server-side validation bounds to
  **inline per-field errors**; show a **saving/disabled** state during the `PATCH`; confirm an
  **explicit success** after write (a guardrail edit the user can't confirm is a safety problem).
- **Pause model:** "Pause" is UI over `blackout_dates`. Support **today-only, a dated range, and
  indefinite-until-cleared**; **list active blackout entries and let the user remove one to
  un-pause**; enabling pause takes a **lightweight confirm** (it stops money movement). Copy follows
  the engine's `BLACKOUT` semantics.
- **Attest, and the unmatched-payment dead end:** a household in `UNMATCHED_PAYMENT` **cannot** be
  attested (KTD-7). Attest shows a **distinct** state from plain "not yet attested" — it names that a
  card the system can't match is blocking, and that there is **no in-app fix this rung** (points to
  support / the next-rung Link UI), rather than silently failing the write.
- **Discoverability:** the existing feed's `CARD_COVERAGE_INCOMPLETE` refusal item gets a **CTA into
  `Attest.tsx`** (the feed data stays read-only) — otherwise the user has no way to reach the screen
  that clears the refusal they're staring at.

## Files

- `mobile/src/screens/Settings.tsx` (new)
- `mobile/src/screens/Attest.tsx` (new)
- a CTA on the existing read-only feed item into `Attest.tsx`
- tests under `mobile/`

## Acceptance criteria

- [ ] A settings change persists and the dashboard reflects the new guardrail.
- [ ] An invalid edit shows an inline error and **no write**.
- [ ] Pause today/range/indefinite each persist and are individually removable.
- [ ] Attesting a clean household clears the coverage refusal in the feed.
- [ ] A household with an unmatched payment shows the **distinct blocked state** and the write is
      refused, not silently dropped.

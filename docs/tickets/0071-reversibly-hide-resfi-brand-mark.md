---
id: "0071"
title: Reversibly hide the ResFi brand mark on status.html and its linked HTML pages
type: chore
status: in-progress
priority: low
repo: cfo-ai
agentId: mobile-rn-agent
agentKind: classic
agentScope: repo
horizon: now
created: 2026-08-19
refs:
  - pr: 96
---

# Hide the ResFi logo, keep it one command from coming back

Remove the visible ResFi brand mark from the static HTML pages, without deleting the markup
or the asset, so it can be restored later with a single search-and-uncomment.

## Scope — where the logo actually is

The only visible ResFi logo is one repeated block: the brand `<img>` pointing at
`resfi-brand.svg`, plus its `.brand` CSS rule. It lives in **4 source pages under `docs/`**
(the source of truth):

| File | `<img class="brand">` | `.brand` CSS rule |
|---|---|---|
| `docs/status.html` | ~L397 | L67 |
| `docs/decision-flow.html` | ~L481 | L66 |
| `docs/explainers/backend-components.html` | ~L297 | L66 |
| `docs/explainers/engine-functions.html` | ~L318 | L67 |

**Out of scope — nothing to change:**

- `mobile/dist/*.html` are **build artifacts** (`mobile/dist` is git-ignored), regenerated from
  `docs/` by `mobile/package.json`'s `copy:docs` (and `build:web`). Do **not** hand-edit them;
  they pick up the change on the next build.
- The mobile Expo app UI (`mobile/App.tsx`, `mobile/src/`) shows **no logo image** — it uses
  brand *colors* only (`mobile/src/theme.ts`). Nothing to remove.
- `web/link.html` (the connect-a-bank dev tool) has **no logo**. Nothing to remove.

## Approach — comment out, don't delete

In each of the 4 `docs/` pages, wrap the `<img class="brand">` block in a labelled marker so
it stops rendering (and stops the SVG fetch) while staying trivially restorable:

```html
<!-- LOGO-HIDDEN: delete these two comment lines to restore the ResFi brand mark
  <img class="brand" src="./design/logos/resfi-brand.svg"
       alt="resfi — responsible finance" width="259" height="88">
-->
```

- **Keep `docs/design/logos/resfi-brand.svg`** in place (and its `copy:docs` line) so restore is
  byte-identical.
- **Leave the `.brand` CSS rule** as-is (harmless once no element uses it), so restoring is only
  about the `<img>`.
- Preferred over `.brand { display: none }`: a display toggle would leave the browser still
  fetching the SVG; commenting out removes both the render and the request.

## Acceptance

1. All 4 `docs/` pages listed above have their `<img class="brand">` block wrapped in the
   `LOGO-HIDDEN` marker; opening each page shows **no** brand mark and issues **no** request for
   `resfi-brand.svg`.
2. `resfi-brand.svg` and the `copy:docs` line that copies it are **unchanged**.
3. `cd mobile && npm run copy:docs` regenerates `mobile/dist/*.html` cleanly and the dist copies
   also render without the logo (spot-check `mobile/dist/status.html`). No committed change to
   `mobile/dist` — it is git-ignored.
4. `git diff` touches **only** the 4 `docs/` source files.

## Restore (document in the PR body)

```
grep -rl LOGO-HIDDEN docs/        # the 4 pages
# in each, remove the two LOGO-HIDDEN comment lines around the <img class="brand">
cd mobile && npm run copy:docs    # regenerate dist
```

## Notes

- No test suite covers these static pages; a rendered spot-check of the 4 pages (and one dist
  copy) is the verification. TDD gate (CLAUDE.md §2) is N/A for a content hide with no behavior
  change — note that in the PR.
- `0070` is reserved for the operator month-grouping scoping note referenced by
  `backend/operator.py:235` and `docs/tickets/0069` — this ticket deliberately takes `0071`.

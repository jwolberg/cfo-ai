# Look & feel reference

Visual reference for the frontend MVP scoped in
[`docs/brainstorms/2026-07-13-decision-engine-frontend-mvp-requirements.md`](../brainstorms/2026-07-13-decision-engine-frontend-mvp-requirements.md).
Source material lives in [`screenshots/`](./screenshots) — add new references there and
describe them below rather than leaving images undocumented.

**Provenance note.** These captures appear to be from Resfi's own marketing site.
They're kept here as a visual reference for matching the target company's design
sensibility in this portfolio/demo build, not as original design work — don't reuse the
literal assets (icon art, copy) outside this internal reference use.

---

## Reference 1 — [`reference-01-built-for-simplicity.png`](./screenshots/reference-01-built-for-simplicity.png)

A single value-pillar card ("Built for Simplicity"), presumably one of several in a
scrollable/gridded set on the marketing site.

**Observed attributes:**

- **Card:** white background, large corner radius (~24px), soft diffuse drop shadow, generous
  padding (~32px). Sits on a light neutral-gray page background.
- **Icon badge:** circular, ~80–96px, blue gradient fill (light blue → medium blue), a
  simple white line icon centered inside (here: a gear with a tap/pointer glyph).
- **Headline:** bold sans-serif, near-black, large (~28–32px), tight line height, left-aligned.
- **Accent rule:** short horizontal bar (~110px × 4px) in mid-blue, sitting between headline
  and body — a section divider, not a progress or link indicator.
- **Body copy:** slate/blue-gray (not pure black), ~17–18px, relaxed line height (~1.5),
  left-aligned, sentence-case, plain prose (no bullets in this card).
- **Overall tone:** calm, uncluttered, confident — one idea per card, nothing competing for
  attention. Consistent with the product's own stated thesis (`docs/strategy.md`'s "the
  absence of a decision" and "conservatism is the strategy").

**How this should inform the MVP:**

- Decision-feed items (`R2`) and summary stats (`R3`) read well as the same card
  shape: white rounded card, one clear number or outcome per card, a short label, and a
  one-line plain-language reason — not a dense table.
- The explain assistant (`R4`–`R6`) should keep this same restraint: one answer at a time,
  generous whitespace, no dashboard-density chat UI.
- Color role mapping for the MVP: blue for informational/neutral states (a sweep, a
  positive stat); a warmer/muted tone (not this blue) should mark refusals, so the two
  outcome types are visually distinct without either reading as an error state — refusals
  are the product working, not a failure (`README.md`).

---

## Reference 2 — [`reference-02-logo-wordmark.png`](./screenshots/reference-02-logo-wordmark.png)

The Resfi logo lockup: leaf mark, lowercase wordmark, tagline.

**Observed attributes:**

- **Mark:** a soft green leaf (rounded, hand-drawn vein detail rather than a hard geometric
  icon), sitting left of the wordmark and roughly the wordmark's cap-to-baseline height.
- **Wordmark:** "resfi." in a heavy, rounded lowercase sans (geometric, generous counters,
  ball-terminal dot on the `i`). Two-tone: "res" in the light/mid blue seen elsewhere in the
  system, "fi" in a deep forest green. The trailing period is the same green as the leaf —
  a full-stop, which reads as finality/decisiveness rather than decoration.
- **Tagline:** "responsible finance", light-weight sans, small (~14px), muted gray-green,
  set flush with the start of the wordmark.
- **Palette read:** the system is blue + green, not blue alone — light blue as the primary
  brand tone, forest green as the grounding/serious counterweight, and a mid sage green for
  the organic mark.

**Exact hexes**, read out of the vector source in [`logos/`](./logos) (below), not eyedropped:

| Role | Hex | Where it appears |
| --- | --- | --- |
| Brand blue | `#4AAFC9` | "res" in the wordmark; the nav CTA fill |
| Deep green | `#085041` | "fi" in the wordmark |
| Leaf green | `#1D9E75` | the leaf mark; the trailing period |
| Tagline gray | `#888780` | "responsible finance" |

Note both `#4AAFC9` and `#1D9E75` sit around 2.5:1 on white — fine for fills, bars, and large
display type, **not** for body text. Anything text-sized needs a darkened companion.

**How this should inform the MVP:**

- Confirms the two-color role split the MVP needs: blue for informational/neutral, and the
  green family (not red/amber) as the second voice. A refusal can be rendered in the deep
  green — deliberate and grounded, not an error color.
- Lowercase, rounded, unhurried type is the brand's register. Headings in the MVP should not
  be shouty or all-caps.

---

## Reference 3 — [`reference-03-nav-cta.png`](./screenshots/reference-03-nav-cta.png)

The right-hand end of the marketing site's top nav: a text link plus the primary CTA.

**Observed attributes:**

- **Text link ("About"):** medium-weight sans, mid-blue, no underline, no chrome — reads as
  secondary/tertiary navigation.
- **Primary button ("Join Waitlist"):** fully pill-shaped (radius = half the height), solid
  desaturated steel-blue fill, white medium-weight label, comfortable horizontal padding
  (roughly 2× the vertical). A subtle soft shadow beneath lifts it off the near-white page.
- **Hierarchy:** exactly one filled element in the nav; everything else is plain text. No
  outlined/ghost button tier is in evidence here.
- **Background:** the page behind is near-white with a faint blue cast, not pure `#fff` —
  which is why the white card in Reference 1 still separates from it.

**How this should inform the MVP:**

- Button language for the MVP: pill radius, solid fill for the single primary action per
  view, plain text links for everything else. Resist adding a third button tier.
- The one-primary-action-per-screen restraint matches the product thesis — the explain
  assistant (`R4`–`R6`) should offer one obvious next step, not a toolbar.

---

## Source assets — [`logos/`](./logos)

Pulled from resfi.ai (the live site's own asset paths) so the palette and mark are exact
rather than sampled from a screenshot:

- `resfi-brand.svg` — the full lockup (leaf + wordmark + tagline). This is the file the hex
  table above was read from.
- `resfi-favicon.svg` — the leaf mark alone.
- `resfi-og.png`, `resfi-apple-touch-icon.png` — raster derivatives, for reference only.

The provenance note at the top of this file applies with full force here: these are Resfi's
marks. They are a **color and shape reference** for this build. Don't render them as the
identity of anything we ship.

---

## Where this has been applied

- [`docs/decision-flow.html`](../decision-flow.html) — restyled onto this system. Cards took
  the large radius, soft diffuse shadow, and borderless white treatment from Reference 1;
  section headings took the 110×4 blue accent bar; reason codes took the pill radius from the
  Reference 3 CTA. **Color roles:** blue marks the sweep (the informational/neutral event) and
  deep green marks refusals — deliberately not red, because a refusal is the engine working,
  which is the page's whole argument.

## Open

- Add further screenshots here as they're captured (typography samples, refusal/empty
  states, mobile nav patterns) before this feeds into planning.
- The wordmark's rounded geometric sans hasn't been identified. If the MVP wants to echo it,
  find the family (or a close free stand-in) before headings get locked in.

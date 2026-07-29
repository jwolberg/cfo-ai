/**
 * The Resfi look, read out of the brand's own vector source (`docs/design/look-and-feel.md`).
 *
 * ## The one colour decision that matters
 *
 * A refusal is **not** an error and must never be coloured like one. The product's whole
 * argument is that declining to move money is the engine working correctly — `strategy.md`
 * calls conservatism the strategy — so a refusal gets the brand's deep green: deliberate,
 * grounded, considered. There is no red anywhere in this file, and adding one would quietly
 * turn the most common outcome in the feed into a failure state.
 *
 * Blue marks a sweep and the informational stats. Two voices, both calm.
 *
 * ## Contrast
 *
 * `brandBlue` and `leafGreen` sit around 2.5:1 on white — fine for fills, bars, and badges,
 * and **not** for text. Each has a darkened companion (`blueText`, `greenText`) that is, and
 * the text roles below use those. This is the trap the design doc calls out explicitly, and
 * it is the kind of thing that looks fine to the person who chose the colour and is
 * unreadable to everyone else.
 */
export const colors = {
  // Brand, exact from `docs/design/logos/resfi-brand.svg`.
  brandBlue: '#4AAFC9',
  deepGreen: '#085041',
  leafGreen: '#1D9E75',

  // The active-tab fill, sampled from the brand's own "Join Waitlist" button. It is a softer
  // blue than `brandBlue` and is deliberately its own token rather than an alias: it came from
  // the button, and if the button moves, this moves with it.
  //
  // **White type on this sits at 2.6:1**, which is below WCAG AA (4.5:1 for 13px). The brand's
  // own button does exactly this, so the tab matches it rather than silently disagreeing with
  // it — but the honest fix is `ink` on the fill (6.2:1), and it is one line away.
  tabActive: '#62ABC3',

  // Text-safe companions. Never use the fills above for type.
  blueText: '#217E96',
  greenText: '#085041', // already dark enough to read as body text

  // Surfaces. The page is near-white with a faint blue cast — which is what lets a pure
  // white card separate from it without needing a border.
  page: '#F2F6F8',
  card: '#FFFFFF',
  border: '#E4EBEF',

  // Type.
  ink: '#16202A',
  body: '#586A75',
  muted: '#888780',
} as const;

export const radius = {
  card: 24,
  // A dense list row. The card's 24 on a 44px row is nearly a pill and reads as a button; this is
  // the same family of shape, scaled to something that is one line tall.
  row: 12,
  pill: 999,
} as const;

export const space = {
  xs: 4,
  sm: 8,
  md: 16,
  lg: 24,
  xl: 32,
} as const;

export const type = {
  display: { fontSize: 30, fontWeight: '700' as const, color: colors.ink },
  heading: { fontSize: 20, fontWeight: '700' as const, color: colors.ink },
  stat: { fontSize: 26, fontWeight: '700' as const, color: colors.ink },
  label: { fontSize: 13, fontWeight: '600' as const, color: colors.muted },
  body: { fontSize: 16, lineHeight: 24, color: colors.body },
  small: { fontSize: 14, lineHeight: 20, color: colors.body },
} as const;

/** The soft, diffuse lift the reference cards have. Works on iOS, Android, and web. */
export const shadow = {
  shadowColor: '#0B2B36',
  shadowOpacity: 0.06,
  shadowRadius: 16,
  shadowOffset: { width: 0, height: 4 },
  elevation: 2,
} as const;

/**
 * One fixed-width column, centred, on every target.
 *
 * No responsive breakpoints: the same layout runs on the simulator, a device, and Expo web.
 * A demo that has to be re-verified at three widths is a demo that gets verified at one.
 */
export const COLUMN_WIDTH = 480;

/** Touch targets. Below this a control is technically present and practically not. */
export const MIN_TAP_TARGET = 44;

/**
 * The hero collapsing into a summary bar as you scroll.
 *
 * **In its own file on purpose.** These tests drive a real scroll event through the FlatList,
 * which fires a `setState` deep inside React Native's ScrollView machinery. Living alongside
 * `Dashboard.test.tsx` they corrupted the shared renderer and took **eight previously-green
 * tests** down with them — tests that had nothing to do with scrolling and passed fine in
 * isolation. A separate file gets a separate module registry and a separate renderer, and the
 * blast radius goes to zero.
 */
import { act, render, screen, waitFor, within } from '@testing-library/react-native';

import type { Decision, DecisionsResponse } from '../api/types';
import { Dashboard } from './Dashboard';
import { DEMO_HOUSEHOLD } from '../api/client';

jest.mock('../api/client', () => ({
  ...jest.requireActual('../api/client'),
  getDecisions: jest.fn(),
}));

const { getDecisions } = jest.requireMock('../api/client');

function decision(): Decision {
  return {
    date: '2026-05-30',
    action: 'sweep',
    amount: '400.00',
    target_debt_id: 'card_demo',
    projected_low_balance: '1154.67',
    reason_codes: ['projection'],
    reasons: [{ code: 'projection', text: 'Your balance is heading for a low of $1,154.67.' }],
    paid_off: false,
    checking_balance: '3847.41',
    savings_balance: '2400.00',
    buffer_floor: '800.00',
    debts: [
      { debt_id: 'card_demo', balance: '3451.64', apr: '0.2399', apr_source: 'reported' },
    ],
    debt_balance: '3451.64',
  };
}

function body(paidOff = false): DecisionsResponse {
  return {
    window: { start: '2026-03-02', end: '2026-05-30', today: '2026-05-30' },
    summary: {
      interest_avoided_total: '6019.10',
      total_swept: '8924.27',
      current_buffer: '800.00',
      targeted_debt_id: 'card_demo',
      targeted_debt_balance: '3451.64',
      starting_debt_balance: '13652.42',
      current_debt_balance: '3451.64',
      sweep_count: 35,
      refuse_count: 55,
      paid_off: paidOff,
    },
    decisions: [decision()],
  };
}

/**
 * Scroll the feed to `y`.
 *
 * **This drives the ScrollView's own `onScroll` prop rather than `fireEvent.scroll`, and that is
 * not laziness.** RNTL gates every event through `isEventEnabled`, which asks the nearest touch
 * responder `onStartShouldSetResponder()`. A ScrollView answers **false** — so RNTL decides the
 * element is not accepting events and drops the scroll on the floor, silently. No error, no
 * warning: the handler simply never runs and the assertion fails as though the feature were
 * broken. (It cost an hour. The component was right the whole time.)
 *
 * `onScroll` is a host prop — it is the exact function React Native itself calls on a real
 * scroll, not a private internal — so invoking it is the same code path, minus the library's
 * gate. Wrapped in `act` because it sets state.
 */
async function scrollTo(y: number) {
  const feed = screen.getByTestId('decision-feed');
  await act(async () => {
    feed.props.onScroll({
      nativeEvent: {
        contentOffset: { x: 0, y },
        contentSize: { height: 2000, width: 400 },
        layoutMeasurement: { height: 800, width: 400 },
      },
    });
  });
}

/**
 * The hero's own measured height. Jest has no layout engine, so `onLayout` never fires on its
 * own and the component falls back to an assumed height — which is exactly the guess this
 * feature refuses to rely on. Firing it explicitly is what makes the thresholds below *derived*
 * rather than magic numbers copied out of the implementation.
 *
 * With a hero of 200px sitting at y=68 inside the header, its bottom edge is 268 — plus the
 * list's 32px top padding, the summary is due at a scroll offset of **300**.
 */
const HERO_Y = 68;
const HERO_HEIGHT = 200;
const HERO_HIDDEN_AT = 32 + HERO_Y + HERO_HEIGHT; // 300

async function open(paidOff = false) {
  getDecisions.mockResolvedValue(body(paidOff));
  await render(<Dashboard householdId={DEMO_HOUSEHOLD} onExplain={jest.fn()} />);
  await waitFor(() => expect(screen.getByText('Beaten the bank out of')).toBeTruthy());

  await act(async () => {
    screen.getByTestId('hero-card').props.onLayout({
      nativeEvent: { layout: { x: 0, y: HERO_Y, width: 400, height: HERO_HEIGHT } },
    });
  });
}

/**
 * The summary bar, if it is on screen.
 *
 * `includeHiddenElements` because the bar sets `pointerEvents="none"` — it must not swallow the
 * scroll gesture of the feed it floats over — and RNTL treats a non-interactive subtree as
 * *hidden*, which is a statement about touch, not about pixels. The user can see it perfectly
 * well; they simply cannot tap it, which is the whole intent.
 */
const summaryBar = () =>
  screen.queryByTestId('paydown-summary', { includeHiddenElements: true });

beforeEach(() => getDecisions.mockReset());

describe('the hero collapses as you scroll', () => {
  it('is absent at the top, and appears once the hero has scrolled away', async () => {
    await open();

    expect(summaryBar()).toBeNull();

    await scrollTo(400);

    expect(summaryBar()).toBeTruthy();
  });

  it('does not appear while any of the hero is still on screen', async () => {
    // The summary is a *replacement* for the hero, not a companion to it. Showing both at once
    // means the same 93% twice on one screen, with the bar sitting on top of the card it is
    // duplicating. It waits until the hero is entirely gone.
    await open();

    await scrollTo(HERO_HIDDEN_AT - 40); // hero's last 40px still showing
    expect(summaryBar()).toBeNull();

    await scrollTo(HERO_HIDDEN_AT - 1); // one pixel of it left
    expect(summaryBar()).toBeNull();

    await scrollTo(HERO_HIDDEN_AT); // gone
    expect(summaryBar()).toBeTruthy();
  });

  it('measures the hero rather than assuming its height', async () => {
    // The threshold is derived from the hero's own layout. Grow the hero — a longer streak line,
    // a wrapped hedge, a bigger font — and the summary waits longer, with nobody editing a
    // constant. A hard-coded pixel threshold silently becomes a lie the first time the card
    // changes, and the symptom is the bar overlapping a hero still on screen.
    await open();

    await act(async () => {
      screen.getByTestId('hero-card').props.onLayout({
        nativeEvent: { layout: { x: 0, y: HERO_Y, width: 400, height: HERO_HEIGHT + 300 } },
      });
    });

    // An offset that hid the *old* hero leaves the taller one still visible.
    await scrollTo(HERO_HIDDEN_AT);
    expect(summaryBar()).toBeNull();

    await scrollTo(HERO_HIDDEN_AT + 300);
    expect(summaryBar()).toBeTruthy();
  });

  it('keeps only the paydown, and drops the rest of the hero', async () => {
    // The summary keeps the one thing worth having on screen: how far down the card is. The
    // streak, the interest figure and its hedge belong to the card you just scrolled past.
    await open();
    await scrollTo(400);

    const summary = summaryBar()!;

    expect(within(summary).getByText('Card paid down')).toBeTruthy();
    expect(within(summary).getByText('75%')).toBeTruthy();
    expect(within(summary).getByText(/\$13,652 when we started/)).toBeTruthy();

    expect(within(summary).queryByText(/payment streak/)).toBeNull();
    expect(within(summary).queryByText('Beaten the bank out of')).toBeNull();
    expect(within(summary).queryByText(/as long as you keep your payments up/)).toBeNull();
  });

  it('does not strobe on and off at the boundary', async () => {
    // Two thresholds, not one. A single boundary flickers the bar on every pixel of scroll
    // jitter, and a thumb resting near it gets a strobe. Collapse at 150, expand at 90 — so
    // anywhere between those, whatever the bar is currently doing, it keeps doing.
    await open();

    await scrollTo(HERO_HIDDEN_AT - 10); // hero still just visible — no bar
    expect(summaryBar()).toBeNull();

    await scrollTo(HERO_HIDDEN_AT); // hero gone — bar appears
    expect(summaryBar()).toBeTruthy();

    await scrollTo(HERO_HIDDEN_AT - 10); // back into the dead zone — must NOT flip straight back
    expect(summaryBar()).toBeTruthy();

    await scrollTo(HERO_HIDDEN_AT - 30); // past the expand threshold (24px) — now it goes
    expect(summaryBar()).toBeNull();
  });

  it('summarises a paid-off card as the banner, never as a bar', async () => {
    await open(true);
    await scrollTo(400);

    const summary = summaryBar()!;

    expect(within(summary).getByText('Card paid off 🎉')).toBeTruthy();
    expect(within(summary).queryByText('Card paid down')).toBeNull();
  });
});

/**
 * The Spending screen — a panel per card, and the bad-news one only exists when the news is bad.
 *
 * The tests that matter here are not "does it render". They are:
 *
 * - The **two obligations stay separate.** They fall due a month apart, and merging them into
 *   one "what you owe" number hides the whole reason card spend became an engine input.
 * - The **card that grew says so.** If charges outran payments, the engine already declines to
 *   claim any interest saved (`0014`), and the product must not then stay silent about why.
 * - **Our failure is not their emptiness.** An unreachable backend rendered as "you spent
 *   nothing" is a lie about the household, told to cover for us.
 * - **Every card appears** (`0031`). `cards[0]` was a defect in the walk (`0027`), in the
 *   artifact (`0030`), and here last of all — a portfolio household must see all three.
 * - **The screen follows the switcher** (`0025`). It used to refuse for three of four households;
 *   the remaining risk is the opposite one, showing the *previous* household's figures under the
 *   new name while a request is in flight.
 */
import { render, screen, waitFor, within } from '@testing-library/react-native';

import { DEMO_HOUSEHOLD, ApiError } from '../api/client';
import type { CardSpend, SpendResponse } from '../api/types';
import { Spending } from './Spending';

jest.mock('../api/client', () => ({
  ...jest.requireActual('../api/client'),
  getSpend: jest.fn(),
}));

const { getSpend } = jest.requireMock('../api/client');

function card(over: Partial<CardSpend> = {}): CardSpend {
  return {
    card_id: 'card_demo',
    this_cycle: {
      statement: { amount: '2240.00', due: '2026-06-14', reserved: true },
      unbilled: { amount: '1240.00', due: '2026-07-14', reserved: false },
      held_back: '2240.00',
    },
    last_cycle: { charged: '1450.00', paid: '1760.00', grew_by: '-310.00' },
    ...over,
  };
}

function spend(over: Partial<SpendResponse> = {}): SpendResponse {
  return {
    as_of: '2026-05-30',
    cards: [card()],
    totals: { statement: '2240.00', unbilled: '1240.00', held_back: '2240.00' },
    normal: {
      rolling_30d_cash: ['1800.00', '2231.00', '1400.00'],
      rolling_30d_card: ['900.00', '1240.00'],
      worst_30d_cash: '2231.00',
      worst_30d_card: '1240.00',
    },
    ...over,
  };
}

beforeEach(() => {
  getSpend.mockReset();
});

// ⚠️ `await render(...)`, never a bare `render(...)`.
//
// This file was the only suite here calling `render()` bare — 7 of them, against 0 in
// Dashboard/Dashboard.scroll/ExplainModal — and it was the only suite that failed on a cold jest
// cache. `render` is not a promise, so the `await` looks like noise; it is not. Awaiting yields to
// the microtask queue and lets React flush its effects, and `screen` is only populated after that
// flush. Without it the first `waitFor` can hit `screen` before the render result is registered and
// die with "`render` function has not been called" — pointing at line 53 while line 51 is the
// render.
//
// It only bites when the machine is slow enough to lose the race (a cold babel transform, a loaded
// CI runner), which is why it survived: warm and local it passes every time. Verified by
// reproduction — cold cache, this file alone, first test fails; add the `await`, and two
// consecutive cold full-suite runs pass.

test('the two obligations are shown separately, because they are due a month apart', async () => {
  getSpend.mockResolvedValue(spend());
  await render(<Spending householdId={DEMO_HOUSEHOLD} />);

  await waitFor(() => expect(screen.getByTestId('this-cycle-card_demo')).toBeTruthy());

  // Both rows exist, each carrying its own amount and its own due date. Neither is a merged
  // "total owed" that would hide the month between them. Scoped per-row on purpose: $2,240 also
  // appears in the held-back line, and a loose getByText would pass against the wrong element.
  const statement = within(screen.getByTestId('row-statement-card_demo'));
  const unbilled = within(screen.getByTestId('row-unbilled-card_demo'));

  expect(statement.getByText(/\$2,240\.00/)).toBeTruthy();
  expect(statement.getByText(/reserved/)).toBeTruthy();

  expect(unbilled.getByText(/\$1,240\.00/)).toBeTruthy();
  expect(unbilled.getByText(/not yet reserved/)).toBeTruthy();
});

test('the reserve is named, so it does not look arbitrary', async () => {
  getSpend.mockResolvedValue(spend());
  await render(<Spending householdId={DEMO_HOUSEHOLD} />);

  await waitFor(() => expect(screen.getByTestId('held-back-card_demo')).toBeTruthy());
  expect(screen.getByText(/holding back \$2,240\.00 of your cash/)).toBeTruthy();
});

test('a household whose card shrank is not shown the warning panel', async () => {
  getSpend.mockResolvedValue(spend());
  await render(<Spending householdId={DEMO_HOUSEHOLD} />);

  await waitFor(() => expect(screen.getByTestId('this-cycle-card_demo')).toBeTruthy());
  expect(screen.queryByTestId('card-grew-card_demo')).toBeNull();
});

test('a card that grew says so, and says a sweep will not fix it', async () => {
  getSpend.mockResolvedValue(
    spend({
      cards: [card({ last_cycle: { charged: '1760.00', paid: '1450.00', grew_by: '310.00' } })],
    }),
  );
  await render(<Spending householdId={DEMO_HOUSEHOLD} />);

  await waitFor(() => expect(screen.getByTestId('card-grew-card_demo')).toBeTruthy());

  expect(screen.getByText(/Your card grew \$310\.00 last cycle/)).toBeTruthy();
  expect(screen.getByText(/the spending is the thing to change/)).toBeTruthy();
});

test('the worst 30-day stretch is the headline of the strip chart', async () => {
  getSpend.mockResolvedValue(spend());
  await render(<Spending householdId={DEMO_HOUSEHOLD} />);

  await waitFor(() => expect(screen.getByTestId('worst-month')).toBeTruthy());

  expect(screen.getByText(/\$2,231/)).toBeTruthy();
  expect(screen.getByTestId('strip-chart')).toBeTruthy();
});

test('an unreachable backend is our failure, not the household having spent nothing', async () => {
  getSpend.mockRejectedValue(new ApiError('network', 'nope'));
  await render(<Spending householdId={DEMO_HOUSEHOLD} />);

  await waitFor(() => expect(screen.getByTestId('spending-failed')).toBeTruthy());

  // Emphatically not "$0.00 spent" or "no spending".
  expect(screen.queryByTestId('this-cycle-card_demo')).toBeNull();
  expect(screen.getByText(/couldn't load your spending/i)).toBeTruthy();
});

test('an empty series does not crash the chart', async () => {
  getSpend.mockResolvedValue(
    spend({
      normal: {
        rolling_30d_cash: [],
        rolling_30d_card: [],
        worst_30d_cash: '0.00',
        worst_30d_card: '0.00',
      },
    }),
  );
  await render(<Spending householdId={DEMO_HOUSEHOLD} />);

  await waitFor(() => expect(screen.getByTestId('worst-month')).toBeTruthy());
  expect(screen.queryByTestId('strip-chart')).toBeNull();
});

// --- ticket 0031: every card, and the household it was asked for ------------------------

const PORTFOLIO = 'hh_semimonthly_portfolio';

function portfolio(): SpendResponse {
  return spend({
    cards: [
      card({ card_id: 'card_a' }),
      card({
        card_id: 'card_b_high',
        this_cycle: {
          statement: { amount: '500.00', due: '2026-06-02', reserved: true },
          unbilled: { amount: '300.00', due: '2026-07-02', reserved: false },
          held_back: '500.00',
        },
      }),
      card({ card_id: 'card_c_high' }),
    ],
    totals: { statement: '4980.00', unbilled: '2780.00', held_back: '4980.00' },
  });
}

test('a portfolio household sees every card, not the first one', async () => {
  getSpend.mockResolvedValue(portfolio());
  await render(<Spending householdId={PORTFOLIO} />);

  await waitFor(() => expect(screen.getByTestId('this-cycle-card_a')).toBeTruthy());

  // `0027` in the walk, `0030` in the artifact, and this screen was the last place it could
  // still have shown up. All three, each with its own panel.
  expect(screen.getByTestId('this-cycle-card_b_high')).toBeTruthy();
  expect(screen.getByTestId('this-cycle-card_c_high')).toBeTruthy();
});

test('each card carries its own due dates, because they do not close together', async () => {
  getSpend.mockResolvedValue(portfolio());
  await render(<Spending householdId={PORTFOLIO} />);

  await waitFor(() => expect(screen.getByTestId('row-statement-card_b_high')).toBeTruthy());

  // Card B closes weeks before card A. A single merged due date would have to pick one and be
  // wrong about the other, which is why the totals panel has no date in it at all.
  expect(within(screen.getByTestId('row-statement-card_a')).getByText(/June 14/)).toBeTruthy();
  expect(within(screen.getByTestId('row-statement-card_b_high')).getByText(/June 2/)).toBeTruthy();
});

test('the portfolio total is shown for many cards and suppressed for one', async () => {
  getSpend.mockResolvedValue(portfolio());
  const view = await render(<Spending householdId={PORTFOLIO} />);

  await waitFor(() => expect(screen.getByTestId('portfolio-totals')).toBeTruthy());
  expect(screen.getByText(/holding back \$4,980\.00 of your cash in total/)).toBeTruthy();

  // With one card the total is that card's own figure repeated — a summary of nothing.
  getSpend.mockResolvedValue(spend());
  view.rerender(<Spending householdId={DEMO_HOUSEHOLD} />);

  await waitFor(() => expect(screen.getByTestId('this-cycle-card_demo')).toBeTruthy());
  expect(screen.queryByTestId('portfolio-totals')).toBeNull();
});

// --- the card reference is a reference, not a name ---------------------------------------
//
// These exist because 13 passing tests went green through the defect: they all assert on
// `testID`s, and a testID does not care what the words say. The first draft rendered
// `CARD_B_HIGH` where the panel's title goes — our primary key, shouted, presented as the name
// of the user's card. Nothing caught it until someone looked at the screen.

test('a portfolio shows the card reference quietly, and never as the panel title', async () => {
  getSpend.mockResolvedValue(portfolio());
  await render(<Spending householdId={PORTFOLIO} />);

  await waitFor(() => expect(screen.getByTestId('this-cycle-card_a')).toBeTruthy());

  // Present — three identical panels have to be tellable apart, and the id is the only handle
  // the API gives us until a real display name arrives with Plaid.
  expect(screen.getByTestId('card-ref-card_b_high')).toBeTruthy();

  // But as an aside, not a headline: the panel is still titled by what it is. And never
  // SHOUTED — `.toUpperCase()` on a database key is what this test exists to prevent.
  const panel = within(screen.getByTestId('this-cycle-card_b_high'));
  expect(panel.getByText('THIS CYCLE')).toBeTruthy();
  expect(panel.queryByText('CARD_B_HIGH')).toBeNull();
});

test('a single-card household is not told the name of its only card', async () => {
  getSpend.mockResolvedValue(spend());
  await render(<Spending householdId={DEMO_HOUSEHOLD} />);

  await waitFor(() => expect(screen.getByTestId('this-cycle-card_demo')).toBeTruthy());

  // Naming the only card is noise. There is nothing to tell apart.
  expect(screen.queryByTestId('card-ref-card_demo')).toBeNull();
  expect(screen.queryByText(/card_demo/i)).toBeNull();
});

test('a growing card in a portfolio says "this card", not a primary key', async () => {
  getSpend.mockResolvedValue(
    spend({
      cards: [
        card({ card_id: 'card_a' }),
        card({
          card_id: 'card_b_low',
          last_cycle: { charged: '1760.00', paid: '1450.00', grew_by: '310.00' },
        }),
      ],
      totals: { statement: '4480.00', unbilled: '2480.00', held_back: '4480.00' },
    }),
  );
  await render(<Spending householdId={PORTFOLIO} />);

  await waitFor(() => expect(screen.getByTestId('card-grew-card_b_low')).toBeTruthy());

  // The panel sits directly under the card it is about, so the reference is already on screen.
  // `card_b_low grew $310.00 last cycle` is a database key in a sentence.
  const grew = within(screen.getByTestId('card-grew-card_b_low'));
  expect(grew.getByText(/This card grew \$310\.00 last cycle/)).toBeTruthy();
  expect(grew.queryByText(/card_b_low grew/)).toBeNull();
});

test('a card with no transaction history says nothing, rather than "grew by $0.00"', async () => {
  // `last_cycle: null` means we have no transactions for this card — not that nothing was
  // charged. Only one of those is safe to print, and it is neither of the two panels.
  getSpend.mockResolvedValue(spend({ cards: [card({ last_cycle: null })] }));
  await render(<Spending householdId={DEMO_HOUSEHOLD} />);

  await waitFor(() => expect(screen.getByTestId('this-cycle-card_demo')).toBeTruthy());
  expect(screen.queryByTestId('card-grew-card_demo')).toBeNull();
});

test('the screen asks for the household it was given, and follows a switch', async () => {
  // Ticket 0025's AC, which could not close until 0031 scoped the route: this tab used to serve
  // the demo household's figures no matter who was selected, and refused for the other three.
  getSpend.mockResolvedValue(portfolio());
  const view = await render(<Spending householdId={PORTFOLIO} />);

  await waitFor(() => expect(screen.getByTestId('this-cycle-card_b_high')).toBeTruthy());
  expect(getSpend).toHaveBeenCalledWith(PORTFOLIO);

  getSpend.mockResolvedValue(spend());
  view.rerender(<Spending householdId={DEMO_HOUSEHOLD} />);

  await waitFor(() => expect(screen.getByTestId('this-cycle-card_demo')).toBeTruthy());
  expect(getSpend).toHaveBeenLastCalledWith(DEMO_HOUSEHOLD);

  // The household we left is gone, not merely covered up.
  expect(screen.queryByTestId('this-cycle-card_b_high')).toBeNull();
});

test('a switch clears the previous household before the new one arrives', async () => {
  // The failure 0025 names: "a bug that looks like working software". Leaving card_b_high on
  // screen under the demo household's name while the request is in flight is exactly that, and
  // it is the shape this screen is most exposed to now that it no longer refuses.
  getSpend.mockResolvedValue(portfolio());
  const view = await render(<Spending householdId={PORTFOLIO} />);

  await waitFor(() => expect(screen.getByTestId('this-cycle-card_b_high')).toBeTruthy());

  // A request that never resolves — the in-flight window, held open.
  getSpend.mockReturnValue(new Promise(() => {}));
  view.rerender(<Spending householdId={DEMO_HOUSEHOLD} />);

  await waitFor(() => expect(screen.getByTestId('spending-loading')).toBeTruthy());
  expect(screen.queryByTestId('this-cycle-card_b_high')).toBeNull();
});

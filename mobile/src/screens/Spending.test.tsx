/**
 * The Spending screen — three panels, and one of them only exists when the news is bad.
 *
 * The tests that matter here are not "does it render". They are:
 *
 * - The **two obligations stay separate.** They fall due a month apart, and merging them into
 *   one "what you owe" number hides the whole reason card spend became an engine input.
 * - The **card that grew says so.** If charges outran payments, the engine already declines to
 *   claim any interest saved (`0014`), and the product must not then stay silent about why.
 * - **Our failure is not their emptiness.** An unreachable backend rendered as "you spent
 *   nothing" is a lie about the household, told to cover for us.
 */
import { render, screen, waitFor, within } from '@testing-library/react-native';

import { ApiError } from '../api/client';
import type { SpendResponse } from '../api/types';
import { Spending } from './Spending';

jest.mock('../api/client', () => ({
  ...jest.requireActual('../api/client'),
  getSpend: jest.fn(),
}));

const { getSpend } = jest.requireMock('../api/client');

function spend(over: Partial<SpendResponse> = {}): SpendResponse {
  return {
    as_of: '2026-05-30',
    this_cycle: {
      statement: { amount: '2240.00', due: '2026-06-14', reserved: true },
      unbilled: { amount: '1240.00', due: '2026-07-14', reserved: false },
      held_back: '2240.00',
    },
    last_cycle: { charged: '1450.00', paid: '1760.00', grew_by: '-310.00' },
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
  await render(<Spending />);

  await waitFor(() => expect(screen.getByTestId('this-cycle')).toBeTruthy());

  // Both rows exist, each carrying its own amount and its own due date. Neither is a merged
  // "total owed" that would hide the month between them. Scoped per-row on purpose: $2,240 also
  // appears in the held-back line, and a loose getByText would pass against the wrong element.
  const statement = within(screen.getByTestId('row-statement'));
  const unbilled = within(screen.getByTestId('row-unbilled'));

  expect(statement.getByText(/\$2,240\.00/)).toBeTruthy();
  expect(statement.getByText(/reserved/)).toBeTruthy();

  expect(unbilled.getByText(/\$1,240\.00/)).toBeTruthy();
  expect(unbilled.getByText(/not yet reserved/)).toBeTruthy();
});

test('the reserve is named, so it does not look arbitrary', async () => {
  getSpend.mockResolvedValue(spend());
  await render(<Spending />);

  await waitFor(() => expect(screen.getByTestId('held-back')).toBeTruthy());
  expect(screen.getByText(/holding back \$2,240\.00 of your cash/)).toBeTruthy();
});

test('a household whose card shrank is not shown the warning panel', async () => {
  getSpend.mockResolvedValue(spend());
  await render(<Spending />);

  await waitFor(() => expect(screen.getByTestId('this-cycle')).toBeTruthy());
  expect(screen.queryByTestId('card-grew')).toBeNull();
});

test('a card that grew says so, and says a sweep will not fix it', async () => {
  getSpend.mockResolvedValue(
    spend({ last_cycle: { charged: '1760.00', paid: '1450.00', grew_by: '310.00' } }),
  );
  await render(<Spending />);

  await waitFor(() => expect(screen.getByTestId('card-grew')).toBeTruthy());

  expect(screen.getByText(/Your card grew \$310\.00 last cycle/)).toBeTruthy();
  expect(screen.getByText(/the spending is the thing to change/)).toBeTruthy();
});

test('the worst 30-day stretch is the headline of the strip chart', async () => {
  getSpend.mockResolvedValue(spend());
  await render(<Spending />);

  await waitFor(() => expect(screen.getByTestId('worst-month')).toBeTruthy());

  expect(screen.getByText(/\$2,231/)).toBeTruthy();
  expect(screen.getByTestId('strip-chart')).toBeTruthy();
});

test('an unreachable backend is our failure, not the household having spent nothing', async () => {
  getSpend.mockRejectedValue(new ApiError('network', 'nope'));
  await render(<Spending />);

  await waitFor(() => expect(screen.getByTestId('spending-failed')).toBeTruthy());

  // Emphatically not "$0.00 spent" or "no spending".
  expect(screen.queryByTestId('this-cycle')).toBeNull();
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
  await render(<Spending />);

  await waitFor(() => expect(screen.getByTestId('worst-month')).toBeTruthy());
  expect(screen.queryByTestId('strip-chart')).toBeNull();
});

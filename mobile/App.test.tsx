/**
 * The tab bar. Two screens, a `useState`, and no router — see `App.tsx`.
 *
 * There was no test here at all until the tabs moved to the top and the active one grew a
 * fill, which are exactly the two things a stray style edit silently reverts.
 */
import { render, screen, waitFor } from '@testing-library/react-native';

import App from './App';
import { getDecisions, getSpend } from './src/api/client';
import { colors } from './src/theme';

jest.mock('./src/api/client');

const decisions = getDecisions as jest.MockedFunction<typeof getDecisions>;
const spend = getSpend as jest.MockedFunction<typeof getSpend>;

beforeEach(() => {
  jest.clearAllMocks();
  // Both screens mount at once (they stay mounted across a tab switch), so both endpoints are
  // hit on first render regardless of which tab is showing.
  decisions.mockResolvedValue({
    window: { start: '2026-03-02', end: '2026-05-30', today: '2026-05-30' },
    summary: {
      interest_avoided_total: '6019.10',
      total_swept: '8924.27',
      current_buffer: '800.00',
      targeted_debt_id: 'card_demo',
      targeted_debt_balance: '3451.64',
      starting_debt_balance: '13652.42',
      sweep_count: 35,
      refuse_count: 55,
      paid_off: false,
    },
    decisions: [],
  });
  spend.mockRejectedValue(new Error('not under test'));
});

/** The `backgroundColor` React Native actually resolved for this element. */
function background(element: { props: Record<string, unknown> }): string | undefined {
  const flat = [element.props.style].flat(Infinity) as Array<Record<string, string> | null>;
  return flat.reduce<string | undefined>(
    (found, layer) => (layer && layer.backgroundColor) || found,
    undefined,
  );
}

describe('the tab bar', () => {
  it('fills the active tab with the brand blue, and leaves the inactive one bare', async () => {
    await render(<App />);
    await waitFor(() => expect(screen.getByTestId('tab-decisions')).toBeTruthy());

    expect(background(screen.getByTestId('tab-decisions'))).toBe(colors.tabActive);
    expect(background(screen.getByTestId('tab-spending'))).toBeUndefined();
  });

  it('sits above the content, not below it', async () => {
    // The tabs used to be the last child of the app shell. They are now the first, and a
    // "move it to the top" that leaves the DOM order alone is not a move.
    await render(<App />);
    // Wait for the Dashboard's fetch to land, or the greeting isn't on screen yet and the
    // comparison below is against -1 — which passes for the wrong reason.
    await waitFor(() => expect(screen.getByText('Your money, working.')).toBeTruthy());

    // A plain `JSON.stringify` throws here: the FlatList keeps its `ListHeaderComponent` React
    // element in props, and that element's `_owner` closes a cycle back to the fiber tree.
    const seen = new WeakSet<object>();
    const flat = JSON.stringify(screen.toJSON(), (key, value) => {
      if (key === '_owner' || key === '_store') return undefined;
      if (typeof value === 'object' && value !== null) {
        if (seen.has(value)) return undefined;
        seen.add(value);
      }
      return value;
    });

    const tabs = flat.indexOf('tab-decisions');
    const content = flat.indexOf('Your money, working.');

    expect(tabs).toBeGreaterThan(-1);
    expect(content).toBeGreaterThan(-1);
    expect(tabs).toBeLessThan(content);
  });
});

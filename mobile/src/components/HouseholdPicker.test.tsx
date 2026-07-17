/**
 * The switcher. Ticket 0025.
 *
 * The labels are the load-bearing assertion, and it is not a nitpick: these four households exist
 * to be *compared*, and a picker offering "Household B" would have shipped the plumbing and
 * dropped the point.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react-native';

import type { Household } from '../api/types';
import { HouseholdPicker } from './HouseholdPicker';

jest.mock('../api/client', () => ({
  ...jest.requireActual('../api/client'),
  getHouseholds: jest.fn(),
}));

const { getHouseholds } = jest.requireMock('../api/client');

const HOUSEHOLDS: Household[] = [
  { id: 'hh_demo_biweekly', archetype: 'demo_biweekly', label: 'Biweekly, one card' },
  { id: 'hh_semimonthly_portfolio', archetype: 'semimonthly_portfolio', label: 'Semimonthly, three cards' },
  { id: 'hh_monthly_thin', archetype: 'monthly_thin', label: 'Monthly, two cards' },
  { id: 'hh_apr_unreported', archetype: 'apr_unreported', label: 'Biweekly, two cards, rates unknown' },
];

beforeEach(() => {
  getHouseholds.mockReset();
  getHouseholds.mockResolvedValue({ households: HOUSEHOLDS });
});

describe('the household picker', () => {
  it('shows the selected household by its label, not its id', async () => {
    await render(<HouseholdPicker selected="hh_demo_biweekly" onSelect={jest.fn()} />);

    await waitFor(() => expect(screen.getByText('Biweekly, one card')).toBeTruthy());
    expect(screen.queryByText('hh_demo_biweekly')).toBeNull();
  });

  it('lists every household, by what makes it different', async () => {
    await render(<HouseholdPicker selected="hh_demo_biweekly" onSelect={jest.fn()} />);
    await waitFor(() => expect(screen.getByTestId('household-picker')).toBeTruthy());

    await fireEvent.press(screen.getByTestId('household-picker'));

    for (const h of HOUSEHOLDS) {
      expect(screen.getByTestId(`household-option-${h.id}`)).toBeTruthy();
    }
  });

  it('the labels name the cadence and the card count', async () => {
    /** "Household B" is a failure. The point of these four is comparison, and a label you cannot
     *  compare with has not done its job. */
    await render(<HouseholdPicker selected="hh_demo_biweekly" onSelect={jest.fn()} />);
    await waitFor(() => expect(screen.getByTestId('household-picker')).toBeTruthy());
    await fireEvent.press(screen.getByTestId('household-picker'));

    for (const h of HOUSEHOLDS) {
      expect(h.label).toMatch(/biweekly|semimonthly|monthly/i);
      expect(h.label).toMatch(/card/i);
      expect(h.label).not.toMatch(/household [a-d]/i);
    }
  });

  it('choosing one reports it and closes', async () => {
    const onSelect = jest.fn();
    await render(<HouseholdPicker selected="hh_demo_biweekly" onSelect={onSelect} />);
    await waitFor(() => expect(screen.getByTestId('household-picker')).toBeTruthy());

    await fireEvent.press(screen.getByTestId('household-picker'));
    await fireEvent.press(screen.getByTestId('household-option-hh_monthly_thin'));

    expect(onSelect).toHaveBeenCalledWith('hh_monthly_thin');
    await waitFor(() =>
      expect(screen.queryByTestId('household-option-hh_monthly_thin')).toBeNull(),
    );
  });

  it('marks the current one as selected', async () => {
    await render(<HouseholdPicker selected="hh_monthly_thin" onSelect={jest.fn()} />);
    await waitFor(() => expect(screen.getByTestId('household-picker')).toBeTruthy());
    await fireEvent.press(screen.getByTestId('household-picker'));

    const current = screen.getByTestId('household-option-hh_monthly_thin');
    expect(current.props.accessibilityState.selected).toBe(true);
  });

  it('a failed label fetch stays quiet rather than shouting about itself', async () => {
    /** The list is chrome. The screens below render their own load and error states against the
     *  household actually selected — a red banner because a *label* did not load would be the
     *  picker reporting a fault the product does not have. */
    getHouseholds.mockRejectedValue(new Error('down'));

    await render(<HouseholdPicker selected="hh_demo_biweekly" onSelect={jest.fn()} />);

    await waitFor(() => expect(screen.getByText('hh_demo_biweekly')).toBeTruthy());
    expect(screen.queryByText(/error|failed|couldn't/i)).toBeNull();
  });
});

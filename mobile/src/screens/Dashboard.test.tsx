/**
 * The dashboard's four states, and the fact that they are four different things.
 *
 * Most of these tests exist because the states are so easy to conflate: an unreachable
 * backend rendered as "no decisions" would tell the household nothing happened when in truth
 * we simply couldn't ask, and a paid-off card rendered as "$0.00" reads like a bug on the one
 * day it is unambiguously good news.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react-native';

import { ApiError } from '../api/client';
import type { Decision, DecisionsResponse } from '../api/types';
import { Dashboard } from './Dashboard';

jest.mock('../api/client', () => ({
  ...jest.requireActual('../api/client'),
  getDecisions: jest.fn(),
}));

const { getDecisions } = jest.requireMock('../api/client');

function decision(over: Partial<Decision> = {}): Decision {
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
    debt_balance: '3451.64',
    debt_id: 'card_demo',
    ...over,
  };
}

function body(over: Partial<DecisionsResponse> = {}): DecisionsResponse {
  return {
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
    decisions: [decision()],
    ...over,
  };
}

beforeEach(() => getDecisions.mockReset());

describe('the dashboard', () => {
  it('opens straight onto the decisions — no login, no onboarding', async () => {
    // Covers AE1. The product's premise is that it has been running without them; asking
    // them to sign in to see that would undercut the whole argument.
    getDecisions.mockResolvedValue(body());

    await render(<Dashboard onExplain={jest.fn()} />);

    await waitFor(() => expect(screen.getByText('Your money, working.')).toBeTruthy());
    expect(screen.queryByText(/sign in/i)).toBeNull();
    expect(screen.queryByText(/log in/i)).toBeNull();
  });

  it('shows the stats above the feed', async () => {
    getDecisions.mockResolvedValue(body());

    await render(<Dashboard onExplain={jest.fn()} />);

    await waitFor(() => expect(screen.getByText('$6,019.10')).toBeTruthy());
    expect(screen.getByText('Beaten the bank out of')).toBeTruthy();
  });

  it('celebrates the number without dropping the condition attached to it', async () => {
    // `engine/interest.py` measures interest-avoided against what the household was *already*
    // paying, and only makes the claim when it can stand behind it. The headline is allowed to
    // celebrate. It is not allowed to promise the money is banked.
    getDecisions.mockResolvedValue(body());

    await render(<Dashboard onExplain={jest.fn()} />);

    await waitFor(() => expect(screen.getByText('$6,019.10')).toBeTruthy());
    expect(
      screen.getByText('in interest, as long as you keep your payments up'),
    ).toBeTruthy();
  });

  it('shows the streak and how far the card has come down', async () => {
    getDecisions.mockResolvedValue(body());

    await render(<Dashboard onExplain={jest.fn()} />);

    await waitFor(() => expect(screen.getByText('🔥 35-payment streak')).toBeTruthy());
    // $13,652.42 -> $3,451.64 is 74.7% of the way down.
    expect(screen.getByText('75%')).toBeTruthy();
    expect(screen.getByText(/\$13,652 when we started/)).toBeTruthy();
  });

  it('does not show a streak before the first payment', async () => {
    // "🔥 0-payment streak" is a taunt, not a reward.
    getDecisions.mockResolvedValue(
      body({ summary: { ...body().summary, sweep_count: 0 } }),
    );

    await render(<Dashboard onExplain={jest.fn()} />);

    await waitFor(() => expect(screen.getByText('Beaten the bank out of')).toBeTruthy());
    expect(screen.queryByText(/payment streak/)).toBeNull();
  });

  it('does not draw a progress bar for a card that is already paid off', async () => {
    getDecisions.mockResolvedValue(
      body({ summary: { ...body().summary, paid_off: true } }),
    );

    await render(<Dashboard onExplain={jest.fn()} />);

    await waitFor(() => expect(screen.getByText('Beaten the bank out of')).toBeTruthy());
    expect(screen.queryByText('Card paid down')).toBeNull();
  });

  it('shows a refusal in plain language, not as an error', async () => {
    // Covers AE2. A refusal is the product working. It reads as a decision, not a failure.
    getDecisions.mockResolvedValue(
      body({
        decisions: [
          decision({
            action: 'refuse',
            amount: '0.00',
            reason_codes: ['no_surplus'],
            reasons: [
              {
                code: 'no_surplus',
                text: "Your balance is heading for a low of $1,154.67 — there's nothing spare.",
              },
            ],
          }),
        ],
      }),
    );

    await render(<Dashboard onExplain={jest.fn()} />);

    await waitFor(() => expect(screen.getByText('No payment today')).toBeTruthy());
    expect(screen.getByText(/nothing spare/)).toBeTruthy();
    expect(screen.queryByText(/error|failed|problem/i)).toBeNull();
  });

  it('shows a spinner, then a fallback, when the backend is unreachable', async () => {
    // Covers AE5. And note what it does NOT say: nothing about the household having no
    // decisions. This failure is ours, and the copy says so.
    getDecisions.mockRejectedValue(new ApiError('timeout', 'no response'));

    await render(<Dashboard onExplain={jest.fn()} />);

    await waitFor(() =>
      expect(screen.getByText(/can't reach your decisions/i)).toBeTruthy(),
    );
    expect(screen.getByText(/nothing has happened to your money/i)).toBeTruthy();
    expect(screen.queryByText(/no decisions in this window/i)).toBeNull();
  });

  it('lets the user retry after a failure', async () => {
    getDecisions.mockRejectedValueOnce(new ApiError('network', 'down'));
    getDecisions.mockResolvedValueOnce(body());

    await render(<Dashboard onExplain={jest.fn()} />);
    await waitFor(() => expect(screen.getByText('Try again')).toBeTruthy());

    await fireEvent.press(screen.getByText('Try again'));

    await waitFor(() => expect(screen.getByText('Your money, working.')).toBeTruthy());
  });

  it('an empty window is not the same as a broken backend', async () => {
    getDecisions.mockResolvedValue(body({ decisions: [] }));

    await render(<Dashboard onExplain={jest.fn()} />);

    await waitFor(() =>
      expect(screen.getByText('No decisions in this window yet.')).toBeTruthy(),
    );
    expect(screen.queryByText(/can't reach/i)).toBeNull();
  });

  it('a paid-off card is a banner, not a $0.00 stat', async () => {
    // $0.00 reads like a bug on the one day it is unambiguously good news.
    //
    // The "Card balance" panel that used to carry this is gone, so the state moved into the
    // hero — and it moved rather than being deleted. A paid-off card with no indication
    // anywhere that it is paid off is the failure this test exists to prevent, and removing a
    // panel is exactly how it would have happened.
    getDecisions.mockResolvedValue(
      body({
        summary: {
          ...body().summary,
          paid_off: true,
          targeted_debt_id: null,
          targeted_debt_balance: '0.00',
        },
      }),
    );

    await render(<Dashboard onExplain={jest.fn()} />);

    await waitFor(() => expect(screen.getByText('Card paid off 🎉')).toBeTruthy());
    expect(screen.queryByText('$0')).toBeNull();
    // And no progress bar: there is nothing left to be partway through.
    expect(screen.queryByText('Card paid down')).toBeNull();
  });

  it('tapping a decision asks for an explanation', async () => {
    const onExplain = jest.fn();
    getDecisions.mockResolvedValue(body());

    await render(<Dashboard onExplain={onExplain} />);
    await waitFor(() => expect(screen.getByText('Paid $400.00')).toBeTruthy());

    await fireEvent.press(screen.getByText('Paid $400.00'));

    expect(onExplain).toHaveBeenCalledWith(expect.objectContaining({ date: '2026-05-30' }));
  });
});

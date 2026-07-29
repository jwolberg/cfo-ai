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
// The demo household id (was client.DEMO_HOUSEHOLD before the identity cutover, ticket 0051).
const DEMO_HOUSEHOLD = 'hh_demo_biweekly';

import type { Decision, DecisionsResponse } from '../api/types';
import { Dashboard } from './Dashboard';

jest.mock('../api/client', () => ({
  ...jest.requireActual('../api/client'),
  getDecisions: jest.fn(),
  getLiveDecision: jest.fn(),
}));

const { getDecisions, getLiveDecision } = jest.requireMock('../api/client');

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
    debts: [
      { debt_id: 'card_demo', balance: '3451.64', apr: '0.2399', apr_source: 'reported' },
    ],
    debt_balance: '3451.64',
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
      current_debt_balance: '3451.64',
      sweep_count: 35,
      refuse_count: 55,
      paid_off: false,
    },
    decisions: [decision()],
    ...over,
  };
}

/**
 * Six days, newest first — the order `GET /decisions` serves them in (`types.ts`), alternating
 * sweep and refuse so the disclosure's tally has something to count.
 */
function week(): Decision[] {
  const days = ['2026-05-30', '2026-05-29', '2026-05-28', '2026-05-27', '2026-05-26', '2026-05-25'];
  return days.map((date, i) =>
    decision({
      date,
      action: i % 2 === 0 ? 'sweep' : 'refuse',
      amount: i % 2 === 0 ? '400.00' : '0.00',
      reasons: [{ code: 'projection', text: `Reason for ${date}.` }],
    }),
  );
}

beforeEach(() => {
  getDecisions.mockReset();
  getLiveDecision.mockReset();
});

describe('the dashboard', () => {
  it('opens straight onto the decisions — no login, no onboarding', async () => {
    // Covers AE1. The product's premise is that it has been running without them; asking
    // them to sign in to see that would undercut the whole argument.
    getDecisions.mockResolvedValue(body());

    await render(<Dashboard householdId={DEMO_HOUSEHOLD} onExplain={jest.fn()} />);

    await waitFor(() => expect(screen.getByText('Your money, working.')).toBeTruthy());
    expect(screen.queryByText(/sign in/i)).toBeNull();
    expect(screen.queryByText(/log in/i)).toBeNull();
  });

  it('shows the stats above the feed', async () => {
    getDecisions.mockResolvedValue(body());

    await render(<Dashboard householdId={DEMO_HOUSEHOLD} onExplain={jest.fn()} />);

    await waitFor(() => expect(screen.getByText('$6,019.10')).toBeTruthy());
    expect(screen.getByText('Beaten the bank out of')).toBeTruthy();
  });

  it('celebrates the number without dropping the condition attached to it', async () => {
    // `engine/interest.py` measures interest-avoided against what the household was *already*
    // paying, and only makes the claim when it can stand behind it. The headline is allowed to
    // celebrate. It is not allowed to promise the money is banked.
    getDecisions.mockResolvedValue(body());

    await render(<Dashboard householdId={DEMO_HOUSEHOLD} onExplain={jest.fn()} />);

    await waitFor(() => expect(screen.getByText('$6,019.10')).toBeTruthy());
    expect(
      screen.getByText('in interest, as long as you keep your payments up'),
    ).toBeTruthy();
  });

  it('shows the streak and how far the card has come down', async () => {
    getDecisions.mockResolvedValue(body());

    await render(<Dashboard householdId={DEMO_HOUSEHOLD} onExplain={jest.fn()} />);

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

    await render(<Dashboard householdId={DEMO_HOUSEHOLD} onExplain={jest.fn()} />);

    await waitFor(() => expect(screen.getByText('Beaten the bank out of')).toBeTruthy());
    expect(screen.queryByText(/payment streak/)).toBeNull();
  });

  it('does not draw a progress bar for a card that is already paid off', async () => {
    getDecisions.mockResolvedValue(
      body({ summary: { ...body().summary, paid_off: true } }),
    );

    await render(<Dashboard householdId={DEMO_HOUSEHOLD} onExplain={jest.fn()} />);

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

    await render(<Dashboard householdId={DEMO_HOUSEHOLD} onExplain={jest.fn()} />);

    await waitFor(() => expect(screen.getByText('No payment today')).toBeTruthy());
    expect(screen.getByText(/nothing spare/)).toBeTruthy();
    expect(screen.queryByText(/error|failed|problem/i)).toBeNull();
  });

  it('shows a spinner, then a fallback, when the backend is unreachable', async () => {
    // Covers AE5. And note what it does NOT say: nothing about the household having no
    // decisions. This failure is ours, and the copy says so.
    getDecisions.mockRejectedValue(new ApiError('timeout', 'no response'));

    await render(<Dashboard householdId={DEMO_HOUSEHOLD} onExplain={jest.fn()} />);

    await waitFor(() =>
      expect(screen.getByText(/can't reach your decisions/i)).toBeTruthy(),
    );
    expect(screen.getByText(/nothing has happened to your money/i)).toBeTruthy();
    expect(screen.queryByText(/no decisions in this window/i)).toBeNull();
  });

  it('lets the user retry after a failure', async () => {
    getDecisions.mockRejectedValueOnce(new ApiError('network', 'down'));
    getDecisions.mockResolvedValueOnce(body());

    await render(<Dashboard householdId={DEMO_HOUSEHOLD} onExplain={jest.fn()} />);
    await waitFor(() => expect(screen.getByText('Try again')).toBeTruthy());

    await fireEvent.press(screen.getByText('Try again'));

    await waitFor(() => expect(screen.getByText('Your money, working.')).toBeTruthy());
  });

  it('an empty window is not the same as a broken backend', async () => {
    getDecisions.mockResolvedValue(body({ decisions: [] }));

    await render(<Dashboard householdId={DEMO_HOUSEHOLD} onExplain={jest.fn()} />);

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

    await render(<Dashboard householdId={DEMO_HOUSEHOLD} onExplain={jest.fn()} />);

    await waitFor(() => expect(screen.getByText('Card paid off 🎉')).toBeTruthy());
    expect(screen.queryByText('$0')).toBeNull();
    // And no progress bar: there is nothing left to be partway through.
    expect(screen.queryByText('Card paid down')).toBeNull();
  });

  it('tapping a decision asks for an explanation', async () => {
    const onExplain = jest.fn();
    getDecisions.mockResolvedValue(body());

    await render(<Dashboard householdId={DEMO_HOUSEHOLD} onExplain={onExplain} />);
    await waitFor(() => expect(screen.getByText('Paid $400.00')).toBeTruthy());

    await fireEvent.press(screen.getByText('Paid $400.00'));

    expect(onExplain).toHaveBeenCalledWith(expect.objectContaining({ date: '2026-05-30' }));
  });
});

/**
 * The feed used to be ninety full-height cards in a row — every dated decision arguing its case at
 * the same volume, which is a lot of screen for a question ("did anything happen?") that the last
 * few days answer. So the recent ones keep their reasoning and the rest fold away.
 *
 * What the collapse must NOT do is hide something the household has to act on. That is the last
 * test here, and it is the reason the dense row is a *rendering* choice rather than a data one.
 */
describe('the feed keeps the recent decisions and folds the rest away', () => {
  it('shows the three most recent in full, and holds the rest back', async () => {
    getDecisions.mockResolvedValue(body({ decisions: week() }));

    await render(<Dashboard householdId={DEMO_HOUSEHOLD} onExplain={jest.fn()} />);

    await waitFor(() => expect(screen.getByText('May 30')).toBeTruthy());
    expect(screen.getByText('May 29')).toBeTruthy();
    expect(screen.getByText('May 28')).toBeTruthy();
    expect(screen.queryByText('May 27')).toBeNull();

    // ...and the three that stay keep the "why", which is half of what the feed is for.
    expect(screen.getByText('Reason for 2026-05-30.')).toBeTruthy();
  });

  it('says how many it is holding back, and what happened on them', async () => {
    // A bare "Show more" makes the user tap to find out whether it is worth tapping. The tally is
    // derived from the hidden days themselves, not from `summary` — which counts the whole window,
    // including the three still on screen.
    getDecisions.mockResolvedValue(body({ decisions: week() }));

    await render(<Dashboard householdId={DEMO_HOUSEHOLD} onExplain={jest.fn()} />);

    await waitFor(() => expect(screen.getByText('Show 3 earlier decisions')).toBeTruthy());
    // Hidden: May 27 refuse, May 26 sweep, May 25 refuse.
    expect(screen.getByText('1 payment · 2 no-payment days')).toBeTruthy();
  });

  it('expands to the whole window, and folds back up again', async () => {
    getDecisions.mockResolvedValue(body({ decisions: week() }));

    await render(<Dashboard householdId={DEMO_HOUSEHOLD} onExplain={jest.fn()} />);
    await waitFor(() => expect(screen.getByText('Show 3 earlier decisions')).toBeTruthy());

    await fireEvent.press(screen.getByText('Show 3 earlier decisions'));
    expect(screen.getByText('May 27')).toBeTruthy();
    expect(screen.getByText('May 25')).toBeTruthy();

    await fireEvent.press(screen.getByText('Hide earlier decisions'));
    expect(screen.queryByText('May 27')).toBeNull();
  });

  it('renders the earlier days as dense rows — the outcome, not the whole argument', async () => {
    // The details stay behind the tap, exactly as they always did for a card. What changes is that
    // an earlier day no longer spends a paragraph making its case unprompted.
    const onExplain = jest.fn();
    getDecisions.mockResolvedValue(body({ decisions: week() }));

    await render(<Dashboard householdId={DEMO_HOUSEHOLD} onExplain={onExplain} />);
    await waitFor(() => expect(screen.getByText('Show 3 earlier decisions')).toBeTruthy());
    await fireEvent.press(screen.getByText('Show 3 earlier decisions'));

    expect(screen.queryByText('Reason for 2026-05-27.')).toBeNull();

    await fireEvent.press(screen.getByText('May 27'));
    expect(onExplain).toHaveBeenCalledWith(expect.objectContaining({ date: '2026-05-27' }));
  });

  it('never compacts away a day that is asking the household to do something', async () => {
    // A coverage refusal carries the only route to the Attest screen (ticket 0052). Folding that
    // into a one-line row would quietly strand a household that cannot be swept for until they
    // confirm their cards — the collapse is about volume, never about reachability.
    const decisions = week();
    decisions[4] = decision({
      date: '2026-05-26',
      action: 'refuse',
      amount: '0.00',
      reason_codes: ['card_coverage_incomplete'],
      reasons: [
        {
          code: 'card_coverage_incomplete',
          text: "We can't confirm your cards.",
          params: { coverage: 'unattested', unmatched: 2 },
        },
      ],
    });
    getDecisions.mockResolvedValue(body({ decisions }));

    await render(
      <Dashboard householdId={DEMO_HOUSEHOLD} onExplain={jest.fn()} onAttest={jest.fn()} />,
    );
    await waitFor(() => expect(screen.getByText('Show 3 earlier decisions')).toBeTruthy());
    await fireEvent.press(screen.getByText('Show 3 earlier decisions'));

    expect(screen.getByTestId('attest-cta-2026-05-26')).toBeTruthy();
    expect(screen.getByText("We can't confirm your cards.")).toBeTruthy();
  });

  it('offers no disclosure when there is nothing behind it', async () => {
    getDecisions.mockResolvedValue(body({ decisions: week().slice(0, 3) }));

    await render(<Dashboard householdId={DEMO_HOUSEHOLD} onExplain={jest.fn()} />);

    await waitFor(() => expect(screen.getByText('May 28')).toBeTruthy());
    expect(screen.queryByText(/earlier decisions/)).toBeNull();
  });
});

describe('a linked household (live decision)', () => {
  const LINKED = 'hh_linked_real';

  it('reads /live-decision, not the graded feed, and renders the single decision card', async () => {
    getLiveDecision.mockResolvedValue({
      today: '2026-07-13',
      decision: decision({
        date: '2026-07-13',
        action: 'refuse',
        amount: '0.00',
        reason_codes: ['card_behavior_unknown'],
        reasons: [{ code: 'card_behavior_unknown', text: "We've not yet seen enough statements." }],
      }),
    });

    await render(<Dashboard householdId={LINKED} linked onExplain={jest.fn()} />);

    // The live decision's own card renders...
    await waitFor(() => expect(screen.getByText('No payment today')).toBeTruthy());
    expect(screen.getByText("We've not yet seen enough statements.")).toBeTruthy();
    // ...from the live endpoint, not the seeded window (which is never fetched for a linked one).
    expect(getLiveDecision).toHaveBeenCalledWith(LINKED);
    expect(getDecisions).not.toHaveBeenCalled();
    // and none of the window-summary rollup hero, which a linked household has no data for.
    expect(screen.queryByText('Card paid down')).toBeNull();
    expect(screen.queryByText('Beaten the bank out of')).toBeNull();
  });

  it('shows a "you could be saving" status panel with a get-started CTA when there is debt', async () => {
    const onGetStarted = jest.fn();
    getLiveDecision.mockResolvedValue({
      today: '2026-07-13',
      decision: decision({ action: 'refuse', paid_off: false, debt_balance: '5701.24' }),
    });

    await render(
      <Dashboard householdId={LINKED} linked onExplain={jest.fn()} onGetStarted={onGetStarted} />,
    );

    await waitFor(() => expect(screen.getByText('You could be saving')).toBeTruthy());
    // the real balance, framed as opportunity — not a fabricated "savings" figure
    expect(screen.getByText('$5,701.24')).toBeTruthy();
    await fireEvent.press(screen.getByTestId('get-started-cta'));
    expect(onGetStarted).toHaveBeenCalled();
  });

  it('celebrates when there is no debt to pay down — and offers no CTA', async () => {
    getLiveDecision.mockResolvedValue({
      today: '2026-07-13',
      decision: decision({ action: 'refuse', paid_off: true, debt_balance: '0.00' }),
    });

    await render(<Dashboard householdId={LINKED} linked onExplain={jest.fn()} onGetStarted={jest.fn()} />);

    await waitFor(() => expect(screen.getByText('No interest to pay 🎉')).toBeTruthy());
    expect(screen.queryByTestId('get-started-cta')).toBeNull();
    expect(screen.queryByText('You could be saving')).toBeNull();
  });
});

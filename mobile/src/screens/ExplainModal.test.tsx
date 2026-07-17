/**
 * The modal: base narration for free, the model only on a follow-up, and no dead ends.
 *
 * The first test is the load-bearing one. If opening a decision ever starts calling the
 * assistant, the most-viewed text in the product becomes the one thing that can hallucinate —
 * and that would be invisible in the UI, because a good model produces text that looks
 * exactly like the engine's.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react-native';

import { ApiError } from '../api/client';
import type { Decision } from '../api/types';
import { ExplainModal } from './ExplainModal';

jest.mock('../api/client', () => ({
  ...jest.requireActual('../api/client'),
  getExplanation: jest.fn(),
  askAssistant: jest.fn(),
}));

const { getExplanation, askAssistant } = jest.requireMock('../api/client');

const SWEEP: Decision = {
  date: '2026-05-30',
  action: 'sweep',
  amount: '400.00',
  target_debt_id: 'card_demo',
  projected_low_balance: '1154.67',
  reason_codes: ['projection', 'interest_avoided'],
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

const NARRATION = {
  ...SWEEP,
  narration: [
    'Your balance is heading for a low of $1,154.67 on 2026-06-05.',
    "That's about $14.13 of interest you won't pay.",
  ],
};

beforeEach(() => {
  getExplanation.mockReset();
  askAssistant.mockReset();
  getExplanation.mockResolvedValue(NARRATION);
});

describe('opening a decision', () => {
  it('shows the engine’s own words and calls no model at all', async () => {
    await render(<ExplainModal decision={SWEEP} onClose={jest.fn()} />);

    await waitFor(() => expect(screen.getByText(NARRATION.narration[0])).toBeTruthy());
    expect(screen.getByText(NARRATION.narration[1])).toBeTruthy();
    expect(askAssistant).not.toHaveBeenCalled();
  });

  it('renders nothing when no decision is selected', async () => {
    await render(<ExplainModal decision={null} onClose={jest.fn()} />);

    expect(screen.queryByText(/paid/i)).toBeNull();
    expect(getExplanation).not.toHaveBeenCalled();
  });

  it('a failed narration fetch does not strand the user', async () => {
    getExplanation.mockRejectedValue(new ApiError('timeout', 'slow'));

    await render(<ExplainModal decision={SWEEP} onClose={jest.fn()} />);

    await waitFor(() => expect(screen.getByText(/couldn't load the explanation/i)).toBeTruthy());
    expect(screen.getByText(/nothing has happened to your money/i)).toBeTruthy();
  });

  it('closes back to the dashboard', async () => {
    const onClose = jest.fn();
    await render(<ExplainModal decision={SWEEP} onClose={onClose} />);
    await waitFor(() => expect(screen.getByText(NARRATION.narration[0])).toBeTruthy());

    await fireEvent.press(screen.getByLabelText('Close'));

    expect(onClose).toHaveBeenCalled();
  });
});

describe('a follow-up question', () => {
  it('sends it and renders the reply', async () => {
    askAssistant.mockResolvedValue({ reply: 'We paid $400.00 that day.', outcome: 'answered' });

    await render(<ExplainModal decision={SWEEP} onClose={jest.fn()} />);
    await waitFor(() => expect(screen.getByText(NARRATION.narration[0])).toBeTruthy());

    await fireEvent.changeText(screen.getByLabelText('Ask a follow-up question'), 'why that much?');
    await fireEvent.press(screen.getByLabelText('Send'));

    await waitFor(() => expect(screen.getByText('We paid $400.00 that day.')).toBeTruthy());
    expect(askAssistant).toHaveBeenCalledWith('why that much?', []);
  });

  it('shows a thinking state while the turn is in flight', async () => {
    // The backend may run several tool round-trips and wait on a model between each. Without
    // this the app looks frozen, and the user sends the question again — which costs a second
    // turn and answers neither.
    let finish!: (value: unknown) => void;
    askAssistant.mockReturnValue(new Promise((resolve) => (finish = resolve)));

    await render(<ExplainModal decision={SWEEP} onClose={jest.fn()} />);
    await waitFor(() => expect(screen.getByText(NARRATION.narration[0])).toBeTruthy());

    await fireEvent.changeText(screen.getByLabelText('Ask a follow-up question'), 'why?');
    // Deliberately NOT awaited: the turn is still in flight by construction, and awaiting the
    // press would wait on the very promise this test is holding open.
    void fireEvent.press(screen.getByLabelText('Send'));

    await waitFor(() => expect(screen.getByText('Looking it up…')).toBeTruthy());

    finish({ reply: 'Because your balance allowed it.', outcome: 'answered' });

    await waitFor(() => expect(screen.queryByText('Looking it up…')).toBeNull());
    expect(screen.getByText('Because your balance allowed it.')).toBeTruthy();
  });

  it('a failed turn is a bubble, not a crash — and the input comes back', async () => {
    askAssistant.mockRejectedValue(new ApiError('network', 'down'));

    await render(<ExplainModal decision={SWEEP} onClose={jest.fn()} />);
    await waitFor(() => expect(screen.getByText(NARRATION.narration[0])).toBeTruthy());

    await fireEvent.changeText(screen.getByLabelText('Ask a follow-up question'), 'why?');
    await fireEvent.press(screen.getByLabelText('Send'));

    await waitFor(() => expect(screen.getByText(/couldn't reach the assistant/i)).toBeTruthy());
    // The modal is still open and still usable. A dead input after one failure is a dead end.
    expect(screen.getByLabelText('Ask a follow-up question').props.editable).not.toBe(false);
  });

  it('carries the conversation so far into the next turn', async () => {
    // There is no server-side session — the client holds the conversation and resends it.
    askAssistant.mockResolvedValue({ reply: 'First answer.', outcome: 'answered' });

    await render(<ExplainModal decision={SWEEP} onClose={jest.fn()} />);
    await waitFor(() => expect(screen.getByText(NARRATION.narration[0])).toBeTruthy());

    await fireEvent.changeText(screen.getByLabelText('Ask a follow-up question'), 'first?');
    await fireEvent.press(screen.getByLabelText('Send'));
    await waitFor(() => expect(screen.getByText('First answer.')).toBeTruthy());

    askAssistant.mockResolvedValue({ reply: 'Second answer.', outcome: 'answered' });
    await fireEvent.changeText(screen.getByLabelText('Ask a follow-up question'), 'second?');
    await fireEvent.press(screen.getByLabelText('Send'));

    await waitFor(() =>
      expect(askAssistant).toHaveBeenLastCalledWith('second?', [
        { role: 'user', content: 'first?' },
        { role: 'assistant', content: 'First answer.' },
      ]),
    );
  });

  it('an empty question is not sent', async () => {
    await render(<ExplainModal decision={SWEEP} onClose={jest.fn()} />);
    await waitFor(() => expect(screen.getByText(NARRATION.narration[0])).toBeTruthy());

    await fireEvent.press(screen.getByLabelText('Send'));

    expect(askAssistant).not.toHaveBeenCalled();
  });
});

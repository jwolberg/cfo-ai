/**
 * Attest — "confirm your cards" (ticket 0052). The load-bearing distinction: an *unattested*
 * household can confirm and clear the gate; an *unmatched_payment* household hits a named dead end,
 * not a silent write failure.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react-native';

import { ApiError } from '../api/client';
import { Attest } from './Attest';

jest.mock('../api/client', () => ({
  ...jest.requireActual('../api/client'),
  attest: jest.fn(),
}));

const { attest } = jest.requireMock('../api/client');

beforeEach(() => {
  attest.mockReset();
  attest.mockResolvedValue({ attested: true, card_fingerprint: 'fp' });
});

describe('the attest screen', () => {
  it('an unattested household can confirm, and the write is made', async () => {
    const onAttested = jest.fn();
    await render(
      <Attest
        visible
        householdId="hh1"
        coverage="unattested"
        unmatched={0}
        onClose={jest.fn()}
        onAttested={onAttested}
      />,
    );

    await fireEvent.press(screen.getByTestId('attest-confirm-button'));

    await waitFor(() => expect(screen.getByTestId('attest-done')).toBeTruthy());
    expect(attest).toHaveBeenCalledWith('hh1');
    expect(onAttested).toHaveBeenCalled();
  });

  it('an unmatched-payment household shows a named dead end and cannot attest', async () => {
    await render(
      <Attest
        visible
        householdId="hh1"
        coverage="unmatched_payment"
        unmatched={1}
        onClose={jest.fn()}
      />,
    );

    // The distinct blocked state — not the plain confirm, and no confirm button to press.
    expect(screen.getByTestId('attest-blocked')).toBeTruthy();
    expect(screen.queryByTestId('attest-confirm-button')).toBeNull();
    expect(screen.getByText(/support/i)).toBeTruthy();
    expect(attest).not.toHaveBeenCalled();
  });

  it('tells a viewer they don’t have permission', async () => {
    attest.mockRejectedValue(new ApiError('unauthorized', 'nope'));
    await render(
      <Attest visible householdId="hh1" coverage="unattested" unmatched={0} onClose={jest.fn()} />,
    );

    await fireEvent.press(screen.getByTestId('attest-confirm-button'));

    await waitFor(() => expect(screen.getByTestId('attest-not-allowed')).toBeTruthy());
  });
});

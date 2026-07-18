/**
 * Settings — the first user-facing write (ticket 0052). The load-bearing claims: an invalid edit
 * shows an inline error and writes **nothing**; a valid one persists and ends in an explicit
 * success; the server's own rejection surfaces inline; a viewer is told they can't; and pause is a
 * confirm-then-add over blackout_dates, each removable.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react-native';

import { ApiError } from '../api/client';
import type { Policy } from '../api/types';
import { Settings } from './Settings';

jest.mock('../api/client', () => ({
  ...jest.requireActual('../api/client'),
  getPolicy: jest.fn(),
  updatePolicy: jest.fn(),
}));

const { getPolicy, updatePolicy } = jest.requireMock('../api/client');

const POLICY: Policy = {
  buffer_floor: '800.00',
  max_sweep: '1600.00',
  max_weekly_sweep: '3200.00',
  min_days_between_sweeps: 7,
  blackout_dates: [],
};

beforeEach(() => {
  getPolicy.mockReset();
  updatePolicy.mockReset();
  getPolicy.mockResolvedValue(POLICY);
  updatePolicy.mockResolvedValue(POLICY);
});

async function open() {
  await render(<Settings visible householdId="hh1" onClose={jest.fn()} />);
  await waitFor(() => expect(screen.getByTestId('settings-save')).toBeTruthy());
}

describe('the settings screen', () => {
  it('prefills the current guardrails', async () => {
    await open();
    expect(screen.getByTestId('field-buffer_floor').props.value).toBe('800.00');
  });

  it('an invalid edit shows an inline error and writes nothing', async () => {
    await open();
    await fireEvent.changeText(screen.getByTestId('field-buffer_floor'), '-5');
    await fireEvent.press(screen.getByTestId('settings-save'));

    expect(screen.getByTestId('field-buffer_floor-error')).toBeTruthy();
    expect(updatePolicy).not.toHaveBeenCalled();
  });

  it('rejects a weekly cap below a single sweep, inline', async () => {
    await open();
    await fireEvent.changeText(screen.getByTestId('field-max_weekly_sweep'), '100.00');
    await fireEvent.press(screen.getByTestId('settings-save'));

    expect(screen.getByTestId('field-max_weekly_sweep-error')).toBeTruthy();
    expect(updatePolicy).not.toHaveBeenCalled();
  });

  it('a valid change persists and confirms with an explicit success', async () => {
    await open();
    await fireEvent.changeText(screen.getByTestId('field-buffer_floor'), '500.00');
    await fireEvent.press(screen.getByTestId('settings-save'));

    await waitFor(() => expect(screen.getByTestId('settings-saved')).toBeTruthy());
    expect(updatePolicy).toHaveBeenCalledWith(
      'hh1',
      expect.objectContaining({ buffer_floor: '500.00', min_days_between_sweeps: 7 }),
    );
  });

  it('surfaces the server’s rejection inline', async () => {
    updatePolicy.mockRejectedValue(new ApiError('validation', 'rejected', ['too low']));
    await open();
    await fireEvent.press(screen.getByTestId('settings-save'));

    await waitFor(() => expect(screen.getByTestId('settings-error')).toBeTruthy());
    expect(screen.getByText('too low')).toBeTruthy();
  });

  it('tells a viewer they don’t have permission', async () => {
    updatePolicy.mockRejectedValue(new ApiError('unauthorized', 'nope'));
    await open();
    await fireEvent.press(screen.getByTestId('settings-save'));

    await waitFor(() => expect(screen.getByTestId('settings-not-allowed')).toBeTruthy());
  });

  it('pause takes a confirm, then lists the day, and it is removable', async () => {
    getPolicy.mockResolvedValue({ ...POLICY, blackout_dates: [] });
    await open();

    await fireEvent.press(screen.getByTestId('pause-today'));
    expect(screen.getByTestId('pause-confirm')).toBeTruthy(); // a lightweight confirm first
    await fireEvent.press(screen.getByTestId('pause-confirm-yes'));

    // The paused day is now listed and individually removable.
    const row = await screen.findByTestId(/^pause-2/);
    expect(row).toBeTruthy();
  });

  it('lists an existing pause and removes it', async () => {
    getPolicy.mockResolvedValue({ ...POLICY, blackout_dates: ['2026-08-01'] });
    await open();

    expect(screen.getByTestId('pause-2026-08-01')).toBeTruthy();
    await fireEvent.press(screen.getByTestId('pause-remove-2026-08-01'));
    await waitFor(() => expect(screen.queryByTestId('pause-2026-08-01')).toBeNull());
  });
});

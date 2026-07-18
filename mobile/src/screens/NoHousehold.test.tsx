/**
 * The empty state (ticket 0051): a verified session with no household gets a calm, honest screen —
 * not a blank one, and not a signup flow (onboarding is the next rung).
 */
import { render, screen } from '@testing-library/react-native';

import { NoHousehold } from './NoHousehold';

describe('the no-household screen', () => {
  it('renders and says the user has no household yet', async () => {
    await render(<NoHousehold />);

    expect(screen.getByTestId('no-household')).toBeTruthy();
    expect(screen.getByText('No household yet')).toBeTruthy();
    expect(screen.getByText(/don.t have access to a household yet/i)).toBeTruthy();
  });

  it('is not a signup flow', async () => {
    /** Create-your-household is the deferred Link rung; this screen must not pretend to offer it. */
    await render(<NoHousehold />);
    expect(screen.queryByText(/sign up|create.*(account|household)|get started/i)).toBeNull();
  });
});

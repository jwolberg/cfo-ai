import { formatDateLong, formatDateShort, formatMoney, formatMoneyRounded } from './format';

describe('money', () => {
  it('groups thousands and keeps both cents', () => {
    expect(formatMoney('8924.27')).toBe('$8,924.27');
    expect(formatMoney('400.00')).toBe('$400.00');
    expect(formatMoney('0.00')).toBe('$0.00');
  });

  it('never loses a cent', () => {
    // The whole reason this is string surgery and not `Number(x).toLocaleString()`. A cent
    // that round-trips through a double is no longer the cent the engine decided on — the
    // same rule `money()` enforces in `engine/models.py`, held all the way to the screen.
    expect(formatMoney('14000.10')).toBe('$14,000.10');
    expect(formatMoney('1154.67')).toBe('$1,154.67');
    expect(formatMoney('0.05')).toBe('$0.05');
  });

  it('drops the cents only where they are noise', () => {
    expect(formatMoneyRounded('3451.64')).toBe('$3,451');
  });
});

describe('dates', () => {
  it('does not drift by a timezone', () => {
    // `new Date('2026-03-02')` is midnight UTC, and every getter then renders it in the
    // viewer's local zone — so west of Greenwich the app would cheerfully show "March 1"
    // for a decision the engine made on March 2. These dates are calendar facts, not
    // instants, and this is the test that says so.
    expect(formatDateLong('2026-03-02')).toContain('March 2');
    expect(formatDateShort('2026-03-02')).toBe('Mar 2');
    expect(formatDateLong('2026-01-01')).toContain('January 1');
    expect(formatDateLong('2026-12-31')).toContain('December 31');
  });

  it('names the weekday', () => {
    expect(formatDateLong('2026-05-30')).toBe('Saturday, May 30');
  });
});

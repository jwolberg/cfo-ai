/**
 * Turning the backend's text into the user's text — without ever touching a float.
 *
 * The backend sends `"8924.27"`. The obvious way to render that with thousands separators is
 * `Number(amount).toLocaleString()`, and it is wrong for the same reason `money()` refuses
 * floats in `engine/models.py`: a cent that round-trips through a double is no longer the
 * cent the engine decided on. The discipline is kept end to end, or it is not kept.
 *
 * So the money formatter is string surgery. It never parses.
 */

/** `"8924.27"` → `"$8,924.27"`. Pure text manipulation; no arithmetic anywhere. */
export function formatMoney(amount: string): string {
  const negative = amount.startsWith('-');
  const [whole, cents = '00'] = (negative ? amount.slice(1) : amount).split('.');

  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const padded = cents.padEnd(2, '0').slice(0, 2);

  return `${negative ? '-' : ''}$${grouped}.${padded}`;
}

/** `"8924.27"` → `"$8,924"`. For headline stats, where the cents are noise. */
export function formatMoneyRounded(amount: string): string {
  return formatMoney(amount).replace(/\.\d{2}$/, '');
}

const MONTHS = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
]; // prettier-ignore

const DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

/**
 * Split an ISO date into its parts **without** going through `new Date(iso)`.
 *
 * `new Date('2026-03-02')` parses as midnight **UTC**, and then every getter renders it in
 * the viewer's local zone — so west of Greenwich the app cheerfully displays "March 1" for a
 * decision the engine made on March 2. The dates in this product are calendar facts, not
 * instants, and they must not drift by a timezone.
 *
 * The `Date` constructed here is a *local* midnight, used only to ask which weekday it was.
 */
function parts(iso: string) {
  const [year, month, day] = iso.split('-').map(Number);
  return { year, month, day, weekday: new Date(year, month - 1, day).getDay() };
}

/** `"2026-03-02"` → `"Monday, March 2"`. */
export function formatDateLong(iso: string): string {
  const { month, day, weekday } = parts(iso);
  return `${DAYS[weekday]}, ${MONTHS[month - 1]} ${day}`;
}

/** `"2026-03-02"` → `"Mar 2"`. For the feed, where the date is a label, not a headline. */
export function formatDateShort(iso: string): string {
  const { month, day } = parts(iso);
  return `${MONTHS[month - 1].slice(0, 3)} ${day}`;
}

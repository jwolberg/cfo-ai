/**
 * The shapes the backend actually sends.
 *
 * Every dollar amount is a `string`, not a `number`, and that is deliberate all the way
 * down. JSON has one number type and it is a double; a cent that round-trips through a
 * double is no longer the cent the engine decided on. The backend serializes `"400.00"`
 * (see `backend/main.py`), and the client formats that text for display without ever
 * parsing it into a float.
 *
 * `Money` is a type alias rather than a bare `string` so the intent survives a reader
 * skimming this file: these are amounts, and they are text on purpose.
 */
export type Money = string;

/** ISO `YYYY-MM-DD`. The demo's dates are calendar facts, not timestamps. */
export type IsoDate = string;

/**
 * One household the demo can be switched to.
 *
 * **Not a customer-facing concept.** A customer has one household and it is theirs; the switcher
 * exists for the reviewer `USERS.md` describes, so that the four archetypes can be compared. The
 * label is written server-side (`backend/readpath.py`) because it is copy, and copy the client
 * invented would be a second place the product's words live.
 */
export interface Household {
  id: string;
  /** Null for a real household — synthetic and real must be tellable apart. */
  archetype: string | null;
  /** What makes this household different, in words. Never "Household B".  */
  label: string;
}

export interface HouseholdsResponse {
  households: Household[];
}

/**
 * A household's guardrails — the set the engine may never exceed (ticket 0052). Read via
 * `GET /policy` to prefill the Settings screen, written via `PATCH /policy` (owner-gated). Money is
 * text, like everywhere; `blackout_dates` is the pause surface.
 */
export interface Policy {
  buffer_floor: Money;
  max_sweep: Money;
  max_weekly_sweep: Money;
  min_days_between_sweeps: number;
  blackout_dates: IsoDate[];
}

/** The body of a policy write. Same shape as `Policy` — every field is sent, so a partial edit
 *  cannot silently drop a guardrail. */
export type PolicyUpdate = Policy;

/** The result of `POST /attest`: whether the household is now attested, and the fingerprint of the
 *  card set it was attested against (ticket 0052). */
export interface AttestResponse {
  attested: boolean;
  card_fingerprint: string;
}

/** The engine has exactly two actions. "Paid off" is a refusal carrying `no_debt`. */
export type Action = 'sweep' | 'refuse';

export interface Reason {
  /** The stable fact. Copy edits change `text`; they never change this. */
  code: string;
  /** Rendered by `engine/explain.py`. The client never writes its own explanation. */
  text: string;
  /** The structured fact behind the sentence, JSON-safe (ticket 0052). Lets the client act on the
   *  reason without parsing prose — e.g. `card_coverage_incomplete` carries `coverage` and
   *  `unmatched`, which the Attest CTA keys on. Optional: not every reason carries params. */
  params?: Record<string, unknown>;
}

/** Where a rate came from. A 23% estimate and a reported 23% are the same number, and only this
 *  tells them apart — so it travels with every APR the client is given. `engine/interest.py`
 *  refuses to price an `estimated` rate, and any UI that renders one as a fact undoes that. */
export type AprSource = 'reported' | 'user_entered' | 'estimated';

/** One card, as the engine saw it on that day. */
export interface Debt {
  debt_id: string;
  balance: Money;
  /** Null when the engine was given no rate at all. `estimated` means we guessed it at 23%. */
  apr: string | null;
  apr_source: AprSource;
}

export interface Decision {
  date: IsoDate;
  action: Action;
  amount: Money;
  target_debt_id: string | null;
  projected_low_balance: Money | null;
  reason_codes: string[];
  reasons: Reason[];
  /** `action === 'refuse' && reason_codes.includes('no_debt')`, derived once, server-side. */
  paid_off: boolean;
  checking_balance: Money;
  savings_balance: Money;
  buffer_floor: Money;
  /** Every card, each with its own rate. A household at 27.99% and 17.99% does not have "a
   *  debt", and the single `debt_balance` this replaced hid which was which. */
  debts: Debt[];
  /** The portfolio total, derived server-side so this and `debts` cannot disagree. */
  debt_balance: Money;
}

export interface Summary {
  interest_avoided_total: Money;
  total_swept: Money;
  current_buffer: Money;
  /** The card the engine most recently aimed at — read from its decisions, never re-derived
   *  from the rates: a transactor can hold the highest APR and still never be a target. */
  targeted_debt_id: string | null;
  targeted_debt_balance: Money;
  /** Portfolio totals on the first and last served day — the denominator and numerator for
   *  "how far down is it". Pair these two, never `starting` against `targeted`: on a portfolio
   *  that compares a total against one card and shows progress that did not happen.
   *
   *  They measure the *cards'* progress, which includes the household's own payments as well
   *  as our sweeps. The copy must not claim we did all of it. */
  starting_debt_balance: Money;
  current_debt_balance: Money;
  sweep_count: number;
  refuse_count: number;
  paid_off: boolean;
}

export interface Window {
  start: IsoDate;
  end: IsoDate;
  /** The demo's "today" — the last served day, not the wall clock. */
  today: IsoDate;
}

export interface DecisionsResponse {
  window: Window;
  summary: Summary;
  /** Newest first. The order the feed reads in. */
  decisions: Decision[];
}

/**
 * `GET /households/{id}/live-decision` — a *linked* household re-decided from its current policy
 * and attestation (ticket 0056). One decision for `today` (the last day we have Plaid data for),
 * not a graded window: there is no `summary`/`window` because nothing has been walked over time.
 * The `decision` itself is the same {@link Decision} shape the feed renders (both come from the
 * backend's one `decision_json`).
 */
export interface LiveDecisionResponse {
  today: IsoDate;
  decision: Decision;
}

export interface ExplainResponse extends Decision {
  /** The engine's own sentences, in order. No LLM was involved in producing these. */
  narration: string[];
}

/**
 * Which of the three things happened — see `backend/assistant.py`.
 *
 * `no_record` and `guard_rejected` read alike to the user and are nothing alike to us: the
 * first means we were asked about a day we have nothing for, the second means the model
 * tried to state something it could not support and was caught. Kept apart here so the UI
 * (and any future logging) can tell them apart even when the copy doesn't.
 */
export type AssistantOutcome =
  | 'answered'
  | 'no_record'
  | 'guard_rejected'
  | 'unavailable'
  | 'rate_limited';

export interface AssistantResponse {
  reply: string;
  outcome: AssistantOutcome;
}

export interface Turn {
  role: 'user' | 'assistant';
  content: string;
}

/**
 * `GET /spend` — comprehension, not a decision.
 *
 * Nothing in this shape feeds the engine. The rolling series is the exact structure that will
 * eventually replace `daily_discretionary_high` in the forecast, rendered a release *before* it
 * is trusted with a decision — so it earns its way in having already been looked at.
 */
export interface Obligation {
  amount: Money;
  due: IsoDate;
  /**
   * Whether the engine is already holding cash back for it.
   *
   * The closed statement is reserved; the unbilled balance is not — it comes due a *month*
   * later, outside the 30-day horizon. Two charges three weeks apart leave checking a month
   * apart, and collapsing them into one "what you owe" number hides precisely that.
   */
  reserved: boolean;
}

export interface ThisCycle {
  statement: Obligation;
  unbilled: Obligation;
  /** "We're holding back $2,240 of your cash for this." The reserve, made legible. */
  held_back: Money;
}

export interface LastCycle {
  charged: Money;
  paid: Money;
  /**
   * Positive means the card **grew**. A sweep will not catch that up — the spending is the
   * thing to change, and the engine already declines to claim any interest saved for them.
   */
  grew_by: Money;
}

export interface Normal {
  /** Every overlapping 30-day total in the trailing window. The strip chart. */
  rolling_30d_cash: Money[];
  rolling_30d_card: Money[];
  worst_30d_cash: Money;
  worst_30d_card: Money;
}

/**
 * One card's spending picture. Ticket 0031.
 *
 * `last_cycle` is nullable, and the difference matters: `null` means we have no transactions
 * for this card, which is not the same claim as "you charged nothing". Only one of those is
 * safe to render as "your card grew by $0.00".
 */
export interface CardSpend {
  card_id: string;
  this_cycle: ThisCycle;
  last_cycle: LastCycle | null;
}

/**
 * The totals across the portfolio — sums of **money only**.
 *
 * There is deliberately no `due` here. A household's cards do not close together, so a single
 * due date would be a fiction, and the whole reason the two obligations are reported separately
 * is that they fall due a month apart. That argument gets stronger with three cards, not weaker.
 */
export interface SpendTotals {
  statement: Money;
  unbilled: Money;
  /** The portfolio reserve. The sum of every card's `held_back`, which is what the engine took. */
  held_back: Money;
}

export interface SpendResponse {
  as_of: IsoDate;
  /** Every card, never just the first one — tickets 0027, 0030, and 0031 in its last home. */
  cards: CardSpend[];
  totals: SpendTotals;
  normal: Normal;
}

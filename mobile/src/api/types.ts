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

/** The engine has exactly two actions. "Paid off" is a refusal carrying `no_debt`. */
export type Action = 'sweep' | 'refuse';

export interface Reason {
  /** The stable fact. Copy edits change `text`; they never change this. */
  code: string;
  /** Rendered by `engine/explain.py`. The client never writes its own explanation. */
  text: string;
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
  debt_balance: Money;
  debt_id: string;
}

export interface Summary {
  interest_avoided_total: Money;
  total_swept: Money;
  current_buffer: Money;
  targeted_debt_id: string | null;
  targeted_debt_balance: Money;
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

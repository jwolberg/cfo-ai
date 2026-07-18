/**
 * The only place in the app that talks to the network.
 *
 * Everything goes through `request()`: the base URL, the API key header, and — the part
 * that matters for the UI — a bounded timeout. No screen constructs a fetch of its own, so
 * there is exactly one place where a request can be made without a deadline, and it doesn't.
 *
 * ## Authenticated by a Stytch session, not a baked key (ticket 0051, U6a)
 *
 * There used to be one `EXPO_PUBLIC_API_KEY` inlined into the bundle here — readable by anyone who
 * inspected the app. It is gone. Every request now carries a **verified Stytch session** as
 * `Authorization: Bearer …`, read from `session.ts` (SecureStore on native, the pre-seeded demo
 * session on web). The token is never logged. `household_id` is no longer a client-side default: it
 * is derived from the caller's membership (`GET /households`) and passed explicitly, because the
 * backend authorizes it against the session (a non-member is refused, `KTD-2`).
 *
 * ## Failures are values, not surprises
 *
 * `ApiError.kind` is the whole vocabulary of things that can go wrong, so a screen can
 * render a timeout differently from a 401 differently from "no decision that day" without
 * pattern-matching on message strings. R13's loading→fallback path depends on `timeout`
 * being distinguishable from `network`, and the explain modal depends on `no_record` being
 * an answer rather than an error.
 */
import { getSessionToken } from './session';
import type {
  AssistantResponse,
  DecisionsResponse,
  ExplainResponse,
  HouseholdsResponse,
  IsoDate,
  SpendResponse,
  Turn,
} from './types';

const BASE_URL = process.env.EXPO_PUBLIC_API_URL ?? 'http://localhost:8000';

/**
 * How long the user stares at a spinner before we admit defeat (R13).
 *
 * Deliberately short. The dashboard is a read from memory on a warm Cloud Run instance — if
 * it hasn't answered in eight seconds it is not slow, it is broken, and saying so beats an
 * indefinite spinner.
 */
export const REQUEST_TIMEOUT_MS = 8_000;

/**
 * The assistant gets much longer: the backend may run several tool round-trips and wait on
 * Anthropic between each. Its own timeout is stricter (`API_TIMEOUT_SECONDS`), so this
 * ceiling exists only to stop a wedged socket from hanging the modal open forever.
 */
export const ASSISTANT_TIMEOUT_MS = 60_000;

export type ApiErrorKind =
  | 'timeout'
  | 'network'
  | 'unauthorized'
  | 'no_record'
  | 'server'
  | 'malformed';

export class ApiError extends Error {
  constructor(
    readonly kind: ApiErrorKind,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(path: string, init: RequestInit = {}, timeoutMs = REQUEST_TIMEOUT_MS) {
  const controller = new AbortController();
  const deadline = setTimeout(() => controller.abort(), timeoutMs);

  // The session token, attached as a bearer credential. A missing token means the request goes out
  // unauthenticated and the backend answers 401 — surfaced below as `unauthorized`, the same as a
  // rejected session — so a signed-out app fails closed rather than leaking a request without auth.
  const token = await getSessionToken();

  let response: Response;
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      ...init,
      signal: controller.signal,
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        'Content-Type': 'application/json',
        ...init.headers,
      },
    });
  } catch (error) {
    // An aborted fetch and a dead network both land here. They are different problems and
    // the user is told different things, so they are separated at the only point where the
    // difference is still knowable.
    const aborted = error instanceof Error && error.name === 'AbortError';
    throw new ApiError(
      aborted ? 'timeout' : 'network',
      aborted ? `No response within ${timeoutMs}ms` : 'Could not reach the service',
    );
  } finally {
    clearTimeout(deadline);
  }

  if (response.status === 401 || response.status === 403) {
    throw new ApiError('unauthorized', 'The app is not authorized to read this data');
  }

  if (response.status === 404) {
    // Not a failure. The backend is telling us it has nothing on record for that day —
    // an honest answer to a reasonable question. Callers render it as such.
    throw new ApiError('no_record', 'No decision on record for that day');
  }

  if (!response.ok) {
    throw new ApiError('server', `The service returned ${response.status}`);
  }

  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError('malformed', 'The service returned something unreadable');
  }
}

/**
 * Every household **this user** may switch to — their memberships, and nothing else.
 *
 * The only call with no household in it, because it is the one you make before you have one. Since
 * the identity cutover (ticket 0048) it is membership-scoped on the backend: a signed-in user sees
 * their own household(s), the demo `viewer` sees the demo ones, and there is no baked
 * `DEMO_HOUSEHOLD` default any more — the app learns which household to read from this list (U6a).
 */
export function getHouseholds(): Promise<HouseholdsResponse> {
  return request<HouseholdsResponse>('/households');
}

/** The served window: the feed (newest first) and the summary stats above it. */
export function getDecisions(householdId: string): Promise<DecisionsResponse> {
  return request<DecisionsResponse>(`/households/${householdId}/decisions`);
}

/**
 * What the household spends, and what their cards are about to take.
 *
 * Comprehension, not a decision — nothing served here feeds the engine.
 *
 * **Scoped, and per card, since ticket 0031.** This was `GET /spend`: no household in the path,
 * because it was the last route the backend had not moved to Postgres, and it reported one
 * arbitrary card as "your card". Both are fixed, which is what closes 0025's Spending tab for
 * all four households instead of one.
 */
export function getSpend(householdId: string): Promise<SpendResponse> {
  return request<SpendResponse>(`/households/${householdId}/spend`);
}

/**
 * Why the engine did what it did on `date`, in plain language.
 *
 * No LLM is involved — this renders `engine/explain.py` server-side. Tapping a decision
 * costs one scoped query, which is why the most-viewed text in the product is also the
 * one thing that cannot hallucinate.
 */
export function getExplanation(date: IsoDate, householdId: string): Promise<ExplainResponse> {
  return request<ExplainResponse>(`/households/${householdId}/decisions/${date}/explain`);
}

/**
 * A follow-up question. The only call that reaches a model, and the only one that costs
 * money.
 *
 * The conversation is held in the client and resent whole each turn — there is no
 * server-side session. The backend re-fetches every decision it cites regardless of what the
 * history says it already knows, because the history is our word, not the engine's.
 *
 * The household travels with the question because the backend loads *that household's* window and
 * hands the model nothing else — so it cannot cite another household's figure, rather than being
 * asked not to.
 */
export function askAssistant(
  message: string,
  history: Turn[],
  householdId: string,
): Promise<AssistantResponse> {
  return request<AssistantResponse>(
    '/assistant/message',
    { method: 'POST', body: JSON.stringify({ household_id: householdId, message, history }) },
    ASSISTANT_TIMEOUT_MS,
  );
}

/**
 * The only place in the app that talks to the network.
 *
 * Everything goes through `request()`: the base URL, the API key header, and — the part
 * that matters for the UI — a bounded timeout. No screen constructs a fetch of its own, so
 * there is exactly one place where a request can be made without a deadline, and it doesn't.
 *
 * ## The API key is public, and that is the accepted trade
 *
 * `EXPO_PUBLIC_*` variables are inlined into the bundle at build time. The key below ships
 * inside the app and anyone who inspects it can read it — that is not an oversight, it is a
 * property of every public client, which cannot hold a secret. The key's job is to deter
 * opportunistic traffic (a crawler that found the bare Cloud Run URL), not to resist a
 * determined reader. What actually bounds the damage when it leaks is the rate cap on the
 * one endpoint that costs money (`backend/assistant.py`) and the fact that the data is
 * synthetic and read-only. See `backend/auth.py` for the same note from the other side.
 *
 * ## Failures are values, not surprises
 *
 * `ApiError.kind` is the whole vocabulary of things that can go wrong, so a screen can
 * render a timeout differently from a 401 differently from "no decision that day" without
 * pattern-matching on message strings. R13's loading→fallback path depends on `timeout`
 * being distinguishable from `network`, and the explain modal depends on `no_record` being
 * an answer rather than an error.
 */
import type {
  AssistantResponse,
  DecisionsResponse,
  ExplainResponse,
  IsoDate,
  Turn,
} from './types';

const BASE_URL = process.env.EXPO_PUBLIC_API_URL ?? 'http://localhost:8000';
const API_KEY = process.env.EXPO_PUBLIC_API_KEY ?? '';

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

  let response: Response;
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      ...init,
      signal: controller.signal,
      headers: {
        'X-API-Key': API_KEY,
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

/** The served window: the feed (newest first) and the summary stats above it. */
export function getDecisions(): Promise<DecisionsResponse> {
  return request<DecisionsResponse>('/decisions');
}

/**
 * Why the engine did what it did on `date`, in plain language.
 *
 * No LLM is involved — this renders `engine/explain.py` server-side. Tapping a decision
 * costs one in-memory lookup, which is why the most-viewed text in the product is also the
 * one thing that cannot hallucinate.
 */
export function getExplanation(date: IsoDate): Promise<ExplainResponse> {
  return request<ExplainResponse>(`/decisions/${date}/explain`);
}

/**
 * A follow-up question. The only call that reaches a model, and the only one that costs
 * money.
 *
 * The conversation is held in the client and resent whole each turn — there is no
 * server-side session. The backend re-fetches every decision it cites regardless of what the
 * history says it already knows, because the history is our word, not the engine's.
 */
export function askAssistant(message: string, history: Turn[]): Promise<AssistantResponse> {
  return request<AssistantResponse>(
    '/assistant/message',
    { method: 'POST', body: JSON.stringify({ message, history }) },
    ASSISTANT_TIMEOUT_MS,
  );
}

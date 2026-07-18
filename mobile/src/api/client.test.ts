/**
 * The client wrapper: does every request carry the session, and does every failure have a name?
 *
 * The failure taxonomy is the point. R13's loading→fallback path needs a timeout to be
 * distinguishable from a dead network, and the explain modal needs "no record" to be an
 * answer rather than an error — so those distinctions are pinned here, at the boundary
 * where they are still knowable.
 *
 * The session is mocked (ticket 0051): the client reads a bearer token from `session.ts`, and here
 * that returns a fixed token so we can assert the `Authorization` header without a real Stytch flow.
 */
import { ApiError, askAssistant, getDecisions, getExplanation } from './client';
import { getSessionToken } from './session';

jest.mock('./session');
const sessionToken = getSessionToken as jest.MockedFunction<typeof getSessionToken>;

const HH = 'hh_demo_biweekly';

const fetchMock = jest.fn();
globalThis.fetch = fetchMock as unknown as typeof fetch;

function respond(status: number, body: unknown = {}) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as Response);
}

beforeEach(() => {
  fetchMock.mockReset();
  sessionToken.mockResolvedValue('session-token-abc');
});

describe('every request', () => {
  it('carries the session as a bearer token, and no baked API key', async () => {
    fetchMock.mockReturnValue(respond(200, { decisions: [] }));

    await getDecisions(HH);

    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers.Authorization).toBe('Bearer session-token-abc');
    expect(init.headers['X-API-Key']).toBeUndefined();
  });

  it('sends no Authorization header when signed out', async () => {
    // A missing token goes out unauthenticated and the backend answers 401 — fail closed, do not
    // fabricate an auth header.
    sessionToken.mockResolvedValue(null);
    fetchMock.mockReturnValue(respond(200, {}));

    await getDecisions(HH);

    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers.Authorization).toBeUndefined();
  });

  it('is bounded by a deadline', async () => {
    // No screen may make a request without one — an indefinite spinner is the failure R13
    // exists to prevent, and it can only be prevented here.
    fetchMock.mockReturnValue(respond(200, {}));

    await getDecisions(HH);

    const [, init] = fetchMock.mock.calls[0];
    expect(init.signal).toBeDefined();
  });
});

describe('failures have names', () => {
  it('an aborted request is a timeout, not a network error', async () => {
    // The user is told different things. The difference is only knowable here.
    const aborted = new Error('aborted');
    aborted.name = 'AbortError';
    fetchMock.mockRejectedValue(aborted);

    await expect(getDecisions(HH)).rejects.toMatchObject({ kind: 'timeout' });
  });

  it('an unreachable backend is a network error', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'));

    await expect(getDecisions(HH)).rejects.toMatchObject({ kind: 'network' });
  });

  it('a 404 is "no record" — an answer, not a failure', async () => {
    fetchMock.mockReturnValue(respond(404, { error: 'no_record' }));

    await expect(getExplanation('2025-01-01', HH)).rejects.toMatchObject({ kind: 'no_record' });
  });

  it('a rejected key is unauthorized', async () => {
    fetchMock.mockReturnValue(respond(401));

    await expect(getDecisions(HH)).rejects.toMatchObject({ kind: 'unauthorized' });
  });

  it('a 500 is a server error', async () => {
    fetchMock.mockReturnValue(respond(500));

    await expect(getDecisions(HH)).rejects.toMatchObject({ kind: 'server' });
  });

  it('every failure is an ApiError, so no screen has to match on message strings', async () => {
    fetchMock.mockReturnValue(respond(500));

    await expect(getDecisions(HH)).rejects.toBeInstanceOf(ApiError);
  });
});

describe('the assistant', () => {
  it('sends the message and the conversation so far', async () => {
    // There is no server-side session. The client holds the conversation and resends it,
    // which is what keeps "the artifact is the only state" true on the backend.
    fetchMock.mockReturnValue(respond(200, { reply: 'ok', outcome: 'answered' }));
    const history = [{ role: 'user' as const, content: 'why?' }];

    await askAssistant('and last Tuesday?', history, HH);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain('/assistant/message');
    expect(init.method).toBe('POST');
    // The household travels with the question (ticket 0024): the backend loads that household's
    // window and hands the model nothing else, so it cannot cite another household's figure.
    expect(JSON.parse(init.body)).toEqual({
      household_id: HH,
      message: 'and last Tuesday?',
      history,
    });
  });

  it('gets longer than the dashboard does', async () => {
    // The backend may run several tool round-trips and wait on Anthropic between each. The
    // dashboard's eight seconds would abort a perfectly healthy answer.
    fetchMock.mockReturnValue(respond(200, { reply: 'ok', outcome: 'answered' }));

    await getDecisions(HH);
    await askAssistant('why?', [], HH);

    const dashboardSignal = fetchMock.mock.calls[0][1].signal;
    const assistantSignal = fetchMock.mock.calls[1][1].signal;
    expect(dashboardSignal).not.toBe(assistantSignal);
  });
});

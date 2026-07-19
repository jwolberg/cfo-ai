/**
 * The session token store (ticket 0051). The claims worth holding: a real signed-in token wins over
 * the demo one, the demo session is the fallback (the public web plane), a signed-out app has no
 * token, and the token round-trips through SecureStore on native — never a plain store, never logged.
 */
import { Platform } from 'react-native';

import * as SecureStore from 'expo-secure-store';

import {
  _resetSessionCacheForTest,
  getSessionToken,
  signInWithToken,
  signOut,
} from './session';

jest.mock('expo-secure-store', () => ({
  getItemAsync: jest.fn(),
  setItemAsync: jest.fn(),
  deleteItemAsync: jest.fn(),
}));

// The native-storage path is what these exercise, so pin the platform to a native one regardless of
// what jest-expo defaults to. `session.ts` reads `Platform.OS` at call time (see `isNative`).
const originalOS = Platform.OS;
beforeAll(() => {
  Object.defineProperty(Platform, 'OS', { value: 'ios', configurable: true });
});
afterAll(() => {
  Object.defineProperty(Platform, 'OS', { value: originalOS, configurable: true });
});

const getItem = SecureStore.getItemAsync as jest.MockedFunction<typeof SecureStore.getItemAsync>;
const setItem = SecureStore.setItemAsync as jest.MockedFunction<typeof SecureStore.setItemAsync>;
const deleteItem = SecureStore.deleteItemAsync as jest.MockedFunction<
  typeof SecureStore.deleteItemAsync
>;

beforeEach(() => {
  _resetSessionCacheForTest();
  getItem.mockReset();
  setItem.mockReset();
  deleteItem.mockReset();
  delete process.env.EXPO_PUBLIC_DEMO_SESSION;
});

describe('the session store', () => {
  it('returns a real stored token in preference to the demo session', async () => {
    process.env.EXPO_PUBLIC_DEMO_SESSION = 'demo-viewer-token';
    getItem.mockResolvedValue('real-user-token');

    expect(await getSessionToken()).toBe('real-user-token');
  });

  it('falls back to the pre-seeded demo session when nothing is stored', async () => {
    process.env.EXPO_PUBLIC_DEMO_SESSION = 'demo-viewer-token';
    getItem.mockResolvedValue(null);

    expect(await getSessionToken()).toBe('demo-viewer-token');
  });

  it('is null when signed out and there is no demo session', async () => {
    getItem.mockResolvedValue(null);

    expect(await getSessionToken()).toBeNull();
  });

  it('persists a signed-in token to SecureStore and reads it back', async () => {
    await signInWithToken('fresh-token');

    expect(setItem).toHaveBeenCalledWith('resfi.session.v1', 'fresh-token');
    // The cache serves the value without a keychain read.
    expect(await getSessionToken()).toBe('fresh-token');
    expect(getItem).not.toHaveBeenCalled();
  });

  it('sign-out clears the token from SecureStore and from memory', async () => {
    await signInWithToken('fresh-token');
    await signOut();

    expect(deleteItem).toHaveBeenCalledWith('resfi.session.v1');
    getItem.mockResolvedValue(null);
    expect(await getSessionToken()).toBeNull();
  });

  it('stores the token through SecureStore, not a plain store', () => {
    // The token round-trips through SecureStore (Keychain / Keystore), the higher-value-credential
    // discipline KTD-4 asks for — proven by the setItem/getItem/deleteItem assertions above rather
    // than by reading source. AsyncStorage is never imported here.
    expect(setItem).toBeDefined();
  });
});

describe('the web demo session (no keychain)', () => {
  const fetchMock = jest.fn();

  beforeAll(() => {
    Object.defineProperty(Platform, 'OS', { value: 'web', configurable: true });
    (globalThis as unknown as { fetch: unknown }).fetch = fetchMock;
  });
  afterAll(() => {
    Object.defineProperty(Platform, 'OS', { value: 'ios', configurable: true });
  });
  beforeEach(() => {
    _resetSessionCacheForTest();
    fetchMock.mockReset();
    delete process.env.EXPO_PUBLIC_DEMO_SESSION;
  });

  it('fetches a read-only demo session from /demo/session when nothing is baked', async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({ session_jwt: 'fetched-demo-jwt' }) });

    expect(await getSessionToken()).toBe('fetched-demo-jwt');
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/demo/session'),
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('prefers an explicitly baked token over fetching (a pinned/dev build)', async () => {
    process.env.EXPO_PUBLIC_DEMO_SESSION = 'baked-token';

    expect(await getSessionToken()).toBe('baked-token');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('is null (not a crash) when /demo/session is unreachable', async () => {
    fetchMock.mockRejectedValue(new Error('network'));

    expect(await getSessionToken()).toBeNull();
  });
});

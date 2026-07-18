/**
 * The session token — the bearer credential that replaced the baked shared key (ticket 0051, U6a).
 *
 * ## Why this exists, and what it is not
 *
 * Before the identity cutover the app shipped one `EXPO_PUBLIC_API_KEY` inside the bundle, readable
 * by anyone (`client.ts`'s old note). That key is gone. In its place, every request carries a
 * **verified Stytch session** (`Authorization: Bearer …`), and this module is the one place the token
 * is read, stored, and cleared.
 *
 * ## Storage: SecureStore on native, a baked demo session on web
 *
 * A session token is a **higher-value bearer credential than the shared key it replaces**, so on a
 * real device it lives in **Expo SecureStore (Keychain / Keystore)** — never `AsyncStorage`, never
 * plain storage — and is **never logged** (KTD-4's discipline, one tier up).
 *
 * SecureStore does not exist on web, and it does not need to: the **public demo** (`cfo-ai-1.web.app`)
 * is not a signed-in user. It carries a pre-seeded, read-only `viewer` session (KTD-10) supplied at
 * build time as `EXPO_PUBLIC_DEMO_SESSION`, so a visitor reaches the demo with no login exactly as
 * before — while a real user signs in on native and gets their own `owner` session. The two planes
 * never cross, which is enforced on the backend (a demo `viewer` can write nothing and can name no
 * non-demo household), not trusted to the client.
 *
 * ## The Stytch sign-in itself is the one deferred integration point
 *
 * `signInWithToken` is the seam a Stytch Expo sign-in flow (magic link / OTP) hands its
 * `session_jwt` to. Wiring the Stytch SDK UI and exercising it on a device is the piece that needs a
 * real Stytch project and a simulator — flagged, not faked. Everything else here is exercised under
 * jest.
 */
import { Platform } from 'react-native';

import * as SecureStore from 'expo-secure-store';

// The SecureStore key. A dotted namespace so it cannot collide with anything else in the keychain.
const STORE_KEY = 'resfi.session.v1';

// The public demo's pre-seeded read-only session, inlined at build time (KTD-10). Empty in a real
// user's native build, where the token comes from sign-in instead. Read at call time (not a module
// const) so it reflects the build's env and is exercisable under test.
function demoSession(): string {
  return process.env.EXPO_PUBLIC_DEMO_SESSION ?? '';
}

// SecureStore is native-only. On web the demo session is the only token, so there is nothing to
// persist and nothing to read from a keychain that isn't there. Evaluated at call time so the choice
// tracks the running platform (and is exercisable under test).
function isNative(): boolean {
  return Platform.OS !== 'web';
}

// `undefined` = not yet loaded; `null` = loaded and signed out; a string = the current token. The
// cache means a warm app does not hit the keychain on every request.
let cached: string | null | undefined;

// SecureStore on native, `null` on web (where the demo session is the only token and there is no
// keychain to reach). The module is imported statically but only *called* behind this guard, so the
// web path never touches it.
function secureStore(): typeof SecureStore | null {
  return isNative() ? SecureStore : null;
}

/**
 * The current session token, or `null` if signed out.
 *
 * Order: the in-memory cache, then SecureStore (native, a real signed-in user), then the baked demo
 * session. A real token always wins over the demo one, so a signed-in native user is never demoted
 * to the read-only demo plane.
 */
export async function getSessionToken(): Promise<string | null> {
  if (cached !== undefined) return cached;

  const store = secureStore();
  if (store) {
    try {
      const stored = await store.getItemAsync(STORE_KEY);
      if (stored) {
        cached = stored;
        return stored;
      }
    } catch {
      // A keychain read that fails falls through to the demo session rather than crashing sign-in.
    }
  }

  cached = demoSession() || null;
  return cached;
}

/** Store a verified session token (the Stytch sign-in seam). Persisted in SecureStore on native. */
export async function signInWithToken(token: string): Promise<void> {
  cached = token;
  const store = secureStore();
  if (store) {
    try {
      await store.setItemAsync(STORE_KEY, token);
    } catch {
      // Memory-only if the keychain refuses; the session still works for this app run.
    }
  }
}

/** Clear the session (sign out). Removes it from SecureStore too. */
export async function signOut(): Promise<void> {
  cached = null;
  const store = secureStore();
  if (store) {
    try {
      await store.deleteItemAsync(STORE_KEY);
    } catch {
      // Nothing to do — the in-memory cache is already cleared.
    }
  }
}

/** Reset the in-memory cache. Tests only — production relies on sign-in/out to move it. */
export function _resetSessionCacheForTest(): void {
  cached = undefined;
}

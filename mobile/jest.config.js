// Jest config, moved out of package.json so the `testTimeout` below can carry its reasoning.
// JSON has no comments, and a bare `"testTimeout": 15000` is indistinguishable from someone
// silencing a flake — which is exactly what it must not be mistaken for.

module.exports = {
  preset: 'jest-expo',

  // Keep the babel transform cache in the repo, so CI can restore it between runs. This is the
  // actual fix; `testTimeout` below is only the safety net for the run that misses this cache.
  cacheDirectory: '<rootDir>/.jest-cache',

  // 30s, not jest's default 5s. **Measured, not guessed** — and the first two numbers I tried
  // were both wrong, which is why this comment exists.
  //
  // The same file, 9 tests:
  //     cold cache  13.86s
  //     warm cache   2.38s
  //
  // ~11.5s of that is jest-expo babel-transforming the React Native + Expo module graph, and it is
  // paid by whichever test happens to render first. That is the whole flake: a *different* test
  // failed on each CI run because jest schedules files differently, and whoever went first ate the
  // transform. 5s failed. 15s also failed. 30s covers the measured cold cost with room for a CI
  // runner slower than this laptop.
  //
  // **This is a CI-speed fix, not a cover for a slow test, and the distinction is checkable.** The
  // failures were jest *test* timeouts — the test never completed. They were NOT `waitFor`
  // assertion failures, which fail at waitFor's own 1000ms default with "unable to find element".
  //
  // Evidence it is environmental rather than ours: run 29364274507 hit the identical timeout on
  // `docs/runbook-0018`, a **documentation-only branch**, on 2026-07-14. A docs branch cannot break
  // React Native. It has now blocked at least three PRs, none of which touched `mobile/`.
  //
  // **If these ever fail with an ASSERTION rather than a timeout, do not raise this number.** That
  // is a real bug, and raising the timeout would hide it.
  testTimeout: 30000,

  transformIgnorePatterns: [
    'node_modules/(?!((jest-)?react-native|@react-native(-community)?|expo(nent)?|@expo(nent)?/.*|@expo-google-fonts/.*|react-navigation|@react-navigation/.*|@sentry/react-native|native-base|react-native-svg))',
  ],
};

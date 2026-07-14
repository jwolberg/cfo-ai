/**
 * The whole app: a dashboard, and a modal over it.
 *
 * No router. This MVP has one screen and one overlay — file-based routing would be
 * scaffolding for navigation that doesn't exist. When a second screen earns its place, so
 * will the router.
 *
 * The selected decision lives here rather than inside the modal, which is what makes every
 * open a fresh mount against a specific decision (see `ExplainModal`): `null` is closed, a
 * decision is open, and there is no third state to get stuck in.
 */
import { StatusBar } from 'expo-status-bar';
import { useState } from 'react';
import { StyleSheet, View } from 'react-native';

import type { Decision } from './src/api/types';
import { Dashboard } from './src/screens/Dashboard';
import { ExplainModal } from './src/screens/ExplainModal';
import { colors } from './src/theme';

export default function App() {
  const [explaining, setExplaining] = useState<Decision | null>(null);

  return (
    <View style={styles.app}>
      <StatusBar style="dark" />
      <Dashboard onExplain={setExplaining} />
      <ExplainModal decision={explaining} onClose={() => setExplaining(null)} />
    </View>
  );
}

const styles = StyleSheet.create({
  app: { flex: 1, backgroundColor: colors.page },
});

/**
 * Two tabs, and a modal over them.
 *
 * ## The "no router" stance is retired, deliberately
 *
 * This file used to argue that file-based routing was scaffolding for navigation that didn't
 * exist, and that was right while there was one screen. There are now two, and they are
 * **co-equal**: comprehension is not a detail of a sweep. Someone who opens this app to find
 * out where their money went is not on a detour from the decision feed — they are doing the
 * other half of the thing the product is for.
 *
 * So the tabs are real. What they are *not* is a dependency: `expo-router` (SDK 57) is a
 * capable framework and it is a great deal of machinery for a boolean. Two screens, no nesting,
 * no deep links, no URL state — a `useState` and two `Pressable`s say exactly what is happening
 * and cost nothing. When a third screen or a shareable link earns it, the router earns it too.
 *
 * The selected decision still lives here rather than inside the modal, which is what makes
 * every open a fresh mount against a specific decision: `null` is closed, a decision is open,
 * and there is no third state to get stuck in.
 */
import { StatusBar } from 'expo-status-bar';
import { useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import type { Decision } from './src/api/types';
import { Dashboard } from './src/screens/Dashboard';
import { ExplainModal } from './src/screens/ExplainModal';
import { Spending } from './src/screens/Spending';
import { MIN_TAP_TARGET, colors, space, type } from './src/theme';

type Tab = 'decisions' | 'spending';

export default function App() {
  const [tab, setTab] = useState<Tab>('decisions');
  const [explaining, setExplaining] = useState<Decision | null>(null);

  return (
    <View style={styles.app}>
      <StatusBar style="dark" />

      {/* Both screens stay mounted. Switching tabs is not a reason to re-fetch, and a
          half-scrolled feed that resets every time you glance at your spending is the kind of
          small betrayal that makes an app feel cheap. */}
      <View style={[styles.screen, tab === 'decisions' ? null : styles.hidden]}>
        <Dashboard onExplain={setExplaining} />
      </View>
      <View style={[styles.screen, tab === 'spending' ? null : styles.hidden]}>
        <Spending />
      </View>

      <View style={styles.tabs}>
        <TabButton
          label="Decisions"
          active={tab === 'decisions'}
          onPress={() => setTab('decisions')}
        />
        <TabButton
          label="Spending"
          active={tab === 'spending'}
          onPress={() => setTab('spending')}
        />
      </View>

      <ExplainModal decision={explaining} onClose={() => setExplaining(null)} />
    </View>
  );
}

function TabButton({
  label,
  active,
  onPress,
}: {
  label: string;
  active: boolean;
  onPress: () => void;
}) {
  return (
    <Pressable
      onPress={onPress}
      style={styles.tab}
      accessibilityRole="tab"
      accessibilityState={{ selected: active }}
      testID={`tab-${label.toLowerCase()}`}
    >
      <Text style={[styles.tabLabel, active ? styles.tabLabelActive : null]}>{label}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  app: { flex: 1, backgroundColor: colors.page },
  screen: { flex: 1 },
  // `display: none` rather than unmounting: see the note above. State survives the switch.
  hidden: { display: 'none' },
  tabs: {
    flexDirection: 'row',
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: colors.border,
    backgroundColor: colors.card,
  },
  tab: {
    flex: 1,
    minHeight: MIN_TAP_TARGET,
    alignItems: 'center',
    justifyContent: 'center',
    paddingVertical: space.sm,
  },
  tabLabel: { ...type.label, color: colors.muted },
  tabLabelActive: { color: colors.greenText },
});

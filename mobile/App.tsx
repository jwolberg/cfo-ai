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

      {/* Both screens stay mounted. Switching tabs is not a reason to re-fetch, and a
          half-scrolled feed that resets every time you glance at your spending is the kind of
          small betrayal that makes an app feel cheap. */}
      <View style={[styles.screen, tab === 'decisions' ? null : styles.hidden]}>
        <Dashboard onExplain={setExplaining} />
      </View>
      <View style={[styles.screen, tab === 'spending' ? null : styles.hidden]}>
        <Spending />
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
      style={[styles.tab, active ? styles.tabActive : null]}
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
  // At the top now, so the border that separates the bar from the content is beneath it.
  tabs: {
    flexDirection: 'row',
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: colors.border,
    backgroundColor: colors.card,
  },
  tab: {
    flex: 1,
    minHeight: MIN_TAP_TARGET,
    alignItems: 'center',
    justifyContent: 'center',
    paddingVertical: space.sm,
  },
  // The active tab is *filled*, not just recoloured — but the shape is untouched: still the
  // full-width rectangle it always was, not the pill the brand button is.
  tabActive: { backgroundColor: colors.tabActive },
  // 17px, not `type.label`'s 13. These are the app's primary navigation, not a caption on a
  // stat.
  //
  // It does **not** rescue the contrast, and it is worth being precise about why: WCAG's
  // relaxed 3:1 bar applies to "large" text, which means >=18.66px *and* bold (700+). At 17px
  // / weight 600 this is still normal text, so white-on-`tabActive` (2.6:1) is still short of
  // the 4.5:1 it needs. 19px and weight 700 would qualify; `ink` on the fill (6.2:1) passes
  // outright at any size. See `theme.ts`.
  tabLabel: { ...type.label, fontSize: 17, color: colors.muted },
  tabLabelActive: { color: '#FFFFFF' },
});

/**
 * The empty state for a verified session that belongs to no household (ticket 0051, U6a).
 *
 * A freshly JIT-provisioned user — a second reviewer, a QA tester — has a real, verified session but
 * no membership yet (fixtures are narrow this rung, and onboarding is the next one). The old app
 * could not reach this state at all: it hard-coded `DEMO_HOUSEHOLD`, so "which household" was never a
 * question. Now that the household comes from `GET /households`, an empty list is a real answer and
 * has to be rendered as one.
 *
 * Deliberately **not** a blank screen (which reads as broken) and **not** a signup flow (create-your-
 * household is the deferred Link rung). Just an honest, calm statement of where things stand.
 */
import { StatusBar } from 'expo-status-bar';
import { StyleSheet, Text, View } from 'react-native';

import { COLUMN_WIDTH, colors, space, type } from '../theme';

export function NoHousehold() {
  return (
    <View style={styles.page} testID="no-household">
      <StatusBar style="dark" />
      <View style={styles.column}>
        <Text style={styles.title}>No household yet</Text>
        <Text style={styles.body}>
          You&rsquo;re signed in, but you don&rsquo;t have access to a household yet. When one is
          shared with you, it&rsquo;ll show up here.
        </Text>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  page: {
    flex: 1,
    backgroundColor: colors.page,
    alignItems: 'center',
    justifyContent: 'center',
    padding: space.lg,
  },
  column: {
    width: '100%',
    maxWidth: COLUMN_WIDTH,
    gap: space.sm,
  },
  title: { ...type.heading },
  body: { ...type.body },
});

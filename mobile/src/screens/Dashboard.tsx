/**
 * The whole product, on one screen.
 *
 * The persona (USERS.md) opens this app to find out whether the system did anything, and to
 * be reassured if it didn't. So: the stats that answer "is this working" above the fold, and
 * the feed of decisions — including all the days nothing happened, and why — below it. No
 * login, no onboarding, no account linking. It is already running (R1/AE1).
 *
 * ## Four states that are not the same state
 *
 * These get conflated constantly, and each conflation is a specific lie:
 *
 * - **Loading** — we don't know yet. A spinner, bounded (R13).
 * - **Unreachable** — we asked and got nothing back. Say so, offer a retry (AE5). This is
 *   *our* failure and must not be dressed up as the household having no decisions.
 * - **Empty** — we asked, and the window genuinely holds no decisions. A different sentence.
 * - **Paid off** — the card is at zero. The targeted-debt stat becomes a banner rather than
 *   a `$0.00` that reads like a bug.
 *
 * A `$0.00` stat on a legitimately zero first day is a fifth thing, and it is fine: it means
 * the engine hasn't moved money yet, which is true and worth showing.
 */
import { useCallback, useEffect, useState } from 'react';
import {
  ActivityIndicator,
  FlatList,
  Pressable,
  StyleSheet,
  Text,
  View,
} from 'react-native';

import { ApiError, getDecisions } from '../api/client';
import type { Decision, DecisionsResponse } from '../api/types';
import { DecisionFeedItem } from '../components/DecisionFeedItem';
import { formatMoney, formatMoneyRounded } from '../format';
import { COLUMN_WIDTH, MIN_TAP_TARGET, colors, radius, shadow, space, type } from '../theme';

type State =
  | { status: 'loading' }
  | { status: 'ready'; data: DecisionsResponse }
  | { status: 'failed'; kind: ApiError['kind'] };

interface Props {
  onExplain: (decision: Decision) => void;
}

export function Dashboard({ onExplain }: Props) {
  const [state, setState] = useState<State>({ status: 'loading' });

  const load = useCallback(async () => {
    setState({ status: 'loading' });
    try {
      setState({ status: 'ready', data: await getDecisions() });
    } catch (error) {
      // The timeout lives in the client, not here (`REQUEST_TIMEOUT_MS`), so there is no
      // path to a spinner that spins forever — the request always resolves one way or the
      // other, and both ways land in this state machine.
      const kind = error instanceof ApiError ? error.kind : 'network';
      setState({ status: 'failed', kind });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (state.status === 'loading') {
    return (
      <View style={styles.centered} accessibilityRole="progressbar">
        <ActivityIndicator size="large" color={colors.brandBlue} />
        <Text style={styles.loadingText}>Loading your decisions…</Text>
      </View>
    );
  }

  if (state.status === 'failed') {
    return <Unreachable onRetry={load} />;
  }

  const { summary, decisions } = state.data;

  return (
    <FlatList
      data={decisions}
      keyExtractor={(decision) => decision.date}
      contentContainerStyle={styles.list}
      style={styles.scroll}
      ListHeaderComponent={<Header summary={summary} />}
      ListEmptyComponent={<Empty />}
      renderItem={({ item }) => <DecisionFeedItem decision={item} onPress={onExplain} />}
    />
  );
}

function Header({ summary }: { summary: DecisionsResponse['summary'] }) {
  return (
    <View>
      <Text style={styles.greeting}>Your money, working.</Text>
      <Text style={styles.subhead}>
        {summary.sweep_count} payments made, {summary.refuse_count} days left alone — each for
        a reason.
      </Text>

      <View style={styles.statCard}>
        <Text style={styles.statLabel}>Interest you won&apos;t pay</Text>
        <Text style={[styles.statValue, { color: colors.blueText }]}>
          {formatMoney(summary.interest_avoided_total)}
        </Text>
      </View>

      <View style={styles.statRow}>
        <View style={[styles.statCard, styles.half]}>
          <Text style={styles.statLabel}>Buffer protected</Text>
          <Text style={styles.statValue}>{formatMoneyRounded(summary.current_buffer)}</Text>
        </View>

        <View style={[styles.statCard, styles.half]}>
          {summary.paid_off ? (
            // Not a $0.00 stat — that reads like a bug on the one day it is unambiguously
            // good news.
            <>
              <Text style={styles.statLabel}>Your card</Text>
              <Text style={[styles.statValue, { color: colors.greenText }]}>Paid off</Text>
            </>
          ) : (
            <>
              <Text style={styles.statLabel}>Card balance</Text>
              <Text style={styles.statValue}>
                {formatMoneyRounded(summary.targeted_debt_balance)}
              </Text>
            </>
          )}
        </View>
      </View>

      <Text style={styles.feedLabel}>Recent decisions</Text>
    </View>
  );
}

function Empty() {
  return (
    <View style={styles.card}>
      <Text style={styles.emptyText}>No decisions in this window yet.</Text>
    </View>
  );
}

function Unreachable({ onRetry }: { onRetry: () => void }) {
  return (
    <View style={styles.centered}>
      <View style={styles.card}>
        <Text style={styles.errorHeadline}>We can&apos;t reach your decisions right now.</Text>
        <Text style={styles.errorBody}>
          Nothing has happened to your money — this is us, not you. Your decisions are safe and
          we&apos;ll show them as soon as we can load them.
        </Text>
        <Pressable
          accessibilityRole="button"
          onPress={onRetry}
          style={({ pressed }) => [styles.button, pressed && styles.pressed]}
        >
          <Text style={styles.buttonLabel}>Try again</Text>
        </Pressable>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  scroll: { flex: 1, backgroundColor: colors.page },
  list: {
    paddingHorizontal: space.lg,
    paddingTop: space.xl,
    paddingBottom: space.xl,
    width: '100%',
    maxWidth: COLUMN_WIDTH,
    alignSelf: 'center',
  },
  centered: {
    flex: 1,
    backgroundColor: colors.page,
    alignItems: 'center',
    justifyContent: 'center',
    padding: space.lg,
  },
  loadingText: { ...type.body, marginTop: space.md },

  greeting: { ...type.display },
  subhead: { ...type.body, marginTop: space.xs, marginBottom: space.lg },

  statRow: { flexDirection: 'row', gap: space.md },
  half: { flex: 1 },
  statCard: {
    backgroundColor: colors.card,
    borderRadius: radius.card,
    padding: space.lg,
    marginBottom: space.md,
    ...shadow,
  },
  statLabel: { ...type.label, marginBottom: space.xs },
  statValue: { ...type.stat },

  feedLabel: { ...type.label, marginTop: space.md, marginBottom: space.sm },

  card: {
    backgroundColor: colors.card,
    borderRadius: radius.card,
    padding: space.lg,
    width: '100%',
    maxWidth: COLUMN_WIDTH,
    ...shadow,
  },
  emptyText: { ...type.body },

  errorHeadline: { ...type.heading, marginBottom: space.sm },
  errorBody: { ...type.body, marginBottom: space.lg },
  button: {
    backgroundColor: colors.brandBlue,
    borderRadius: radius.pill,
    minHeight: MIN_TAP_TARGET,
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: space.xl,
  },
  buttonLabel: { color: '#FFFFFF', fontSize: 16, fontWeight: '600' },
  pressed: { opacity: 0.8 },
});

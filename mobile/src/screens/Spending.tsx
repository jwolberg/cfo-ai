/**
 * What normal looks like, and what the card is about to take.
 *
 * Comprehension, not a decision. Nothing this screen renders feeds the engine — and the strip
 * chart at the bottom is the exact data structure that will eventually replace
 * `daily_discretionary_high` in the forecast, shown a release *before* it is trusted with a
 * decision. It earns its way in having already been looked at by real households.
 *
 * ## Why the two obligations are separated
 *
 * They come due a **month apart**. The statement that has closed is legally due inside the
 * 30-day horizon and the engine is already holding cash back for it. The unbilled balance is
 * next month's bill, forming now, and nothing is reserved against it yet. Collapse them into
 * one "what you owe" figure and you hide the single thing this screen exists to show.
 *
 * ## The card that grew is not an error
 *
 * If charges outran payments, that panel is the most important thing in the app for that
 * household — and it is still not styled like a failure. `theme.ts` has no red in it on
 * purpose, and the reason applies here too: this is the product telling the truth about their
 * spending, not the app reporting a fault. Deep green, the same voice as a refusal.
 */
import { useEffect, useState } from 'react';
import { ActivityIndicator, ScrollView, StyleSheet, Text, View } from 'react-native';

import { ApiError, DEMO_HOUSEHOLD, getSpend } from '../api/client';
import type { SpendResponse } from '../api/types';
import { formatDateLong, formatMoney, formatMoneyRounded } from '../format';
import { COLUMN_WIDTH, colors, radius, shadow, space, type } from '../theme';

type State =
  | { status: 'loading' }
  | { status: 'ready'; data: SpendResponse }
  | { status: 'failed'; kind: ApiError['kind'] };

interface Props {
  /** Which household. Owned by `App.tsx` — see its note on why this is not local state. */
  householdId: string;
}

/** Text in, boolean out. The string is never parsed for *display* — only compared. */
function isPositive(amount: string): boolean {
  return Number.parseFloat(amount) > 0;
}

export function Spending({ householdId }: Props) {
  const [state, setState] = useState<State>({ status: 'loading' });
  const servable = householdId === DEMO_HOUSEHOLD;

  useEffect(() => {
    if (!servable) return;

    let live = true;
    getSpend()
      .then((data) => {
        if (live) setState({ status: 'ready', data });
      })
      .catch((error: unknown) => {
        if (!live) return;
        const kind = error instanceof ApiError ? error.kind : 'network';
        setState({ status: 'failed', kind });
      });
    return () => {
      live = false;
    };
  }, [servable]);

  // The tab ticket 0025 warns about by name: "the easiest thing to leave pointing at a stale
  // household — a bug that looks like working software."
  //
  // `GET /spend` has no household in it. It is the one route the backend has not moved to
  // Postgres, because its figures come from the whole transaction history and there is no
  // transactions table yet (ticket 0031). So for any household but the demo's, the honest thing
  // is to say so. Rendering the demo's spending under another household's name would be exactly
  // the bug the ticket names, and it would look perfect.
  if (!servable) {
    return (
      <View style={styles.centered} testID="spending-unavailable">
        <Text style={styles.unavailableTitle}>We can't show spending for this household yet</Text>
        <Text style={styles.unavailableBody}>
          The decision feed works for all four. Spending is still wired to the demo household
          only — it reads a transaction history the other three don't have stored yet.
        </Text>
      </View>
    );
  }

  if (state.status === 'loading') {
    return (
      <View style={styles.centered} testID="spending-loading">
        <ActivityIndicator color={colors.brandBlue} />
      </View>
    );
  }

  if (state.status === 'failed') {
    // Our failure, not theirs. Never dressed up as "you have no spending".
    return (
      <View style={styles.centered} testID="spending-failed">
        <Text style={styles.body}>We couldn&apos;t load your spending just now.</Text>
      </View>
    );
  }

  const { this_cycle: cycle, last_cycle: last, normal } = state.data;

  return (
    <ScrollView style={styles.screen} contentContainerStyle={styles.content}>
      <View style={styles.column}>
        {/* --- This cycle ------------------------------------------------------------- */}
        <View style={styles.card} testID="this-cycle">
          <Text style={styles.label}>THIS CYCLE</Text>
          <Text style={styles.stat}>{formatMoney(cycle.unbilled.amount)} charged</Text>
          <Text style={styles.body}>Due {formatDateLong(cycle.unbilled.due)}.</Text>

          <View style={styles.rule} />

          <Row
            label="Statement — already closed"
            note={`Due ${formatDateLong(cycle.statement.due)} · reserved`}
            amount={cycle.statement.amount}
            testID="row-statement"
          />
          <Row
            label="Unbilled — since it closed"
            note={`Due ${formatDateLong(cycle.unbilled.due)} · not yet reserved`}
            amount={cycle.unbilled.amount}
            testID="row-unbilled"
          />

          {/* The line that stops the reserve looking arbitrary. */}
          <Text style={styles.reserve} testID="held-back">
            We&apos;re holding back {formatMoney(cycle.held_back)} of your cash for this.
          </Text>
        </View>

        {/* --- When the sweep is not the answer --------------------------------------- */}
        {isPositive(last.grew_by) && (
          <View style={[styles.card, styles.grew]} testID="card-grew">
            <Text style={styles.heading}>
              Your card grew {formatMoney(last.grew_by)} last cycle.
            </Text>
            <Text style={styles.body}>
              You charged {formatMoney(last.charged)} and paid {formatMoney(last.paid)}. A sweep
              will not catch that up — the spending is the thing to change.
            </Text>
          </View>
        )}

        {/* --- The biggest month: the reserve, made legible ----------------------------- */}
        {/* "Worst" is the engine's word for this number and it stays the engine's word — the
            field is still `worst_30d_cash`, and the reserve really is built against a bad
            case. But it is the household's *own* spending, and telling someone their life is
            the worst kind of it is a judgement we have not earned and do not mean. Highest is
            the same fact without the verdict. */}
        <View style={styles.card} testID="worst-month">
          <Text style={styles.label}>YOUR HIGHEST 30 DAYS</Text>
          <Text style={styles.stat}>{formatMoneyRounded(normal.worst_30d_cash)}</Text>
          <Text style={styles.body}>We reserve against months like that one.</Text>

          <StripChart series={normal.rolling_30d_cash} worst={normal.worst_30d_cash} />

          <Text style={styles.small}>
            Every overlapping 30-day total from your own history — not an average, and not a
            guess at your biggest month.
          </Text>
        </View>
      </View>
    </ScrollView>
  );
}

function Row({
  label,
  note,
  amount,
  testID,
}: {
  label: string;
  note: string;
  amount: string;
  testID: string;
}) {
  return (
    <View style={styles.row} testID={testID}>
      <View style={styles.rowText}>
        <Text style={styles.rowLabel}>{label}</Text>
        <Text style={styles.small}>{note}</Text>
      </View>
      <Text style={styles.rowAmount}>{formatMoney(amount)}</Text>
    </View>
  );
}

/**
 * Every overlapping 30-day total, as bars, with the worst one marked.
 *
 * Hand-rolled from `View`s rather than a charting dependency: it is one bar per window and a
 * colour on the tallest. A chart library would be more code than the chart, and a new package
 * in the bundle for it.
 */
function StripChart({ series, worst }: { series: string[]; worst: string }) {
  if (series.length === 0) return null;

  const peak = Number.parseFloat(worst) || 1;

  return (
    <View style={styles.chart} testID="strip-chart">
      {series.map((value, i) => (
        <View
          key={i}
          style={[
            styles.bar,
            { height: Math.max(2, (Number.parseFloat(value) / peak) * 56) },
            value === worst ? styles.barWorst : null,
          ]}
        />
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.page },
  content: { padding: space.md, paddingBottom: space.xl, alignItems: 'center' },
  column: { width: '100%', maxWidth: COLUMN_WIDTH },
  centered: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: colors.page,
    padding: space.lg,
  },
  card: {
    backgroundColor: colors.card,
    borderRadius: radius.card,
    padding: space.lg,
    marginBottom: space.md,
    ...shadow,
  },
  // Not a warning colour. See the header note: there is no red in this product, and a
  // household whose card is growing is being told the truth, not shown a fault.
  grew: { borderLeftWidth: 4, borderLeftColor: colors.deepGreen },
  // Not red either, and not an error: nothing failed. This is the product being straight about
  // what it has not built yet, which is the same voice as a refusal.
  unavailableTitle: { ...type.body, color: colors.ink, textAlign: 'center' },
  unavailableBody: {
    ...type.label,
    color: colors.muted,
    textAlign: 'center',
    marginTop: space.sm,
    maxWidth: COLUMN_WIDTH,
  },
  label: { ...type.label },
  stat: { ...type.stat, marginTop: space.xs },
  heading: { ...type.heading },
  body: { ...type.body, marginTop: space.xs },
  small: { ...type.small, color: colors.muted },
  rule: {
    height: StyleSheet.hairlineWidth,
    backgroundColor: colors.border,
    marginVertical: space.md,
  },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingVertical: space.sm,
  },
  rowText: { flex: 1, paddingRight: space.sm },
  rowLabel: { ...type.body, color: colors.ink },
  rowAmount: { ...type.body, color: colors.ink, fontVariant: ['tabular-nums'] },
  reserve: { ...type.body, color: colors.greenText, marginTop: space.md },
  chart: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    height: 60,
    marginTop: space.md,
    gap: 1,
  },
  bar: { flex: 1, backgroundColor: colors.border, borderRadius: 1 },
  barWorst: { backgroundColor: colors.brandBlue },
});

/**
 * What normal looks like, and what the cards are about to take.
 *
 * Comprehension, not a decision. Nothing this screen renders feeds the engine — and the strip
 * chart at the bottom is the exact data structure that will eventually replace
 * `daily_discretionary_high` in the forecast, shown a release *before* it is trusted with a
 * decision. It earns its way in having already been looked at by real households.
 *
 * ## It follows the switcher now, and that is ticket 0031
 *
 * This screen used to refuse for three of the four households: `GET /spend` had no household in
 * it, served the demo's figures, and reported one arbitrary card as "your card". 0025 called that
 * tab out by name — "the easiest thing to leave pointing at a stale household, a bug that looks
 * like working software" — and the honest stopgap was a panel saying so. The route is scoped and
 * per-card now, so the panel is gone and the tab follows the switch like every other screen.
 *
 * ## Why the two obligations are separated
 *
 * They come due a **month apart**. The statement that has closed is legally due inside the
 * 30-day horizon and the engine is already holding cash back for it. The unbilled balance is
 * next month's bill, forming now, and nothing is reserved against it yet. Collapse them into
 * one "what you owe" figure and you hide the single thing this screen exists to show.
 *
 * With three cards that argument gets stronger, not weaker — they do not close together — which
 * is why each card carries its own panel and its own dates, and the portfolio total is a sum of
 * money only. There is no combined due date to print, because there is no such date.
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

import { ApiError, getSpend } from '../api/client';
import type { CardSpend, SpendResponse } from '../api/types';
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

  useEffect(() => {
    let live = true;

    // Back to loading on every switch. Without this the previous household's figures stay on
    // screen under the new household's name while the request is in flight — which is 0025's
    // "bug that looks like working software" in its other form, and this screen's whole history.
    setState({ status: 'loading' });

    getSpend(householdId)
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
  }, [householdId]);

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

  const { cards, totals, normal } = state.data;
  const many = cards.length > 1;

  return (
    <ScrollView style={styles.screen} contentContainerStyle={styles.content}>
      <View style={styles.column}>
        {/* --- The portfolio, when there is one ---------------------------------------- */}
        {/* Only for more than one card: with a single card these totals are that card's own
            figures repeated, which is a summary of nothing. */}
        {many && (
          <View style={styles.card} testID="portfolio-totals">
            <Text style={styles.label}>ACROSS YOUR {cards.length} CARDS</Text>
            <Text style={styles.stat}>{formatMoney(totals.statement)} due</Text>
            <Text style={styles.body}>
              Plus {formatMoney(totals.unbilled)} still forming. They come due on different days —
              each card is below.
            </Text>

            <Text style={styles.reserve} testID="held-back-total">
              We&apos;re holding back {formatMoney(totals.held_back)} of your cash in total.
            </Text>
          </View>
        )}

        {/* --- Each card ---------------------------------------------------------------- */}
        {cards.map((card) => (
          <CardPanel key={card.card_id} card={card} showName={many} />
        ))}

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

/**
 * One card: what it owes, what is forming behind that, and whether it grew.
 *
 * ## The card reference is a reference, not a name
 *
 * `card_id` is **our primary key**, not something the user calls their card. A real display name is
 * a Plaid field this product does not have, and inventing one ("Card 1 of 3") would be worse than
 * having none — it is a name we made up, presented as theirs.
 *
 * So on a portfolio it appears, because three identical panels have to be tellable apart and the id
 * is the only handle the API gives us — but in the **quiet register**, beside the label rather than
 * as the title. The first draft shouted `CARD_B_HIGH` where the panel's name goes, which is a
 * database key wearing a headline. `readpath.Household.label` exists because *"a label is copy, and
 * copy that lives in a database is copy nobody can grep for"*; the same argument reaches here, and
 * until there is real copy to show, the honest thing is to show the id as what it is.
 *
 * On a single-card household it is omitted entirely: naming the only card is noise.
 */
function CardPanel({ card, showName }: { card: CardSpend; showName: boolean }) {
  const { this_cycle: cycle, last_cycle: last } = card;

  return (
    <>
      <View style={styles.card} testID={`this-cycle-${card.card_id}`}>
        <View style={styles.cardHead}>
          <Text style={styles.label}>THIS CYCLE</Text>
          {showName && (
            <Text style={styles.cardRef} testID={`card-ref-${card.card_id}`}>
              {card.card_id}
            </Text>
          )}
        </View>
        <Text style={styles.stat}>{formatMoney(cycle.unbilled.amount)} charged</Text>
        <Text style={styles.body}>Due {formatDateLong(cycle.unbilled.due)}.</Text>

        <View style={styles.rule} />

        <Row
          label="Statement — already closed"
          note={`Due ${formatDateLong(cycle.statement.due)} · reserved`}
          amount={cycle.statement.amount}
          testID={`row-statement-${card.card_id}`}
        />
        <Row
          label="Unbilled — since it closed"
          note={`Due ${formatDateLong(cycle.unbilled.due)} · not yet reserved`}
          amount={cycle.unbilled.amount}
          testID={`row-unbilled-${card.card_id}`}
        />

        {/* The line that stops the reserve looking arbitrary. This card's own share of it —
            `obligation_in_horizon` per card, not the portfolio total apportioned. */}
        <Text style={styles.reserve} testID={`held-back-${card.card_id}`}>
          We&apos;re holding back {formatMoney(cycle.held_back)} of your cash for this.
        </Text>
      </View>

      {/* --- When the sweep is not the answer --------------------------------------- */}
      {/* `last === null` means we have no transaction history for this card, which is not the
          same as "nothing was charged" — so there is nothing to say, and we say nothing. */}
      {last !== null && isPositive(last.grew_by) && (
        <View style={[styles.card, styles.grew]} testID={`card-grew-${card.card_id}`}>
          {/* "This card", never `card_b_low` — the panel sits directly under the card it is about,
              so the reference is already on screen and a primary key in a sentence is not copy. */}
          <Text style={styles.heading}>
            {showName ? 'This card grew' : 'Your card grew'} {formatMoney(last.grew_by)} last cycle.
          </Text>
          <Text style={styles.body}>
            You charged {formatMoney(last.charged)} and paid {formatMoney(last.paid)}. A sweep will
            not catch that up — the spending is the thing to change.
          </Text>
        </View>
      )}
    </>
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
  // The label and the card reference share a baseline: "THIS CYCLE   card_b_high". The label is
  // the heading; the id is an aside next to it, which is the whole point of not making it the title.
  cardHead: { flexDirection: 'row', alignItems: 'baseline', justifyContent: 'space-between' },
  // Muted, lowercase, tabular. It is an identifier — it should read like one, not like a name we
  // are claiming is theirs. See CardPanel's note.
  cardRef: { ...type.small, color: colors.muted, fontVariant: ['tabular-nums'] },
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

/**
 * One decision, as one card — or, for the days folded away behind the feed's disclosure, as one
 * dense row.
 *
 * The reference cards do one idea each, and so does this: the outcome, the day, and the
 * engine's own first sentence about why. Everything else is behind the tap.
 *
 * The refusal is the case worth getting right. It is the most common outcome in the feed and
 * it is the product working — so it reads as a considered decision in the brand's deep green,
 * with a headline that says what happened to the money rather than what didn't happen. "No
 * payment today" is a statement; "Failed" or a red badge would be a lie about a system that
 * behaved exactly as designed.
 *
 * ## Telling the two outcomes apart at a glance
 *
 * The outcome colour lives on a **spine down the leading edge** — blue for a payment, deep green
 * for a day we left alone. It replaced a short accent bar that sat inline before the date, which
 * read as a stray dash in front of the date rather than as a status. A spine cannot be misread as
 * punctuation: it is the edge of the card, it is the full height of the row, and in the dense list
 * it stacks into a rail you can scan without reading a word.
 *
 * The headline carries the same colour, so each item has one accent rather than two competing ones.
 */
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { formatDateShort, formatMoney } from '../format';
import { MIN_TAP_TARGET, colors, radius, shadow, space, type } from '../theme';
import type { Decision } from '../api/types';

/** The coverage sub-state and unmatched count the Attest screen needs, carried on the
 *  `card_coverage_incomplete` reason's params (ticket 0052). */
export interface CoverageRefusal {
  coverage: string;
  unmatched: number;
}

interface Props {
  decision: Decision;
  onPress: (decision: Decision) => void;
  /** Open the Attest screen for a coverage-incomplete refusal (ticket 0052). Optional: without it,
   *  the CTA does not render — a household with no coverage refusal never sees one anyway. */
  onAttest?: (refusal: CoverageRefusal) => void;
  /**
   * Render as a dense row instead of a full card — the shape the feed's folded-away days take.
   *
   * A **request**, not a command: a day carrying a coverage refusal renders full-size regardless,
   * because its "Confirm your cards" CTA is the household's only route to the Attest screen and a
   * one-line row has nowhere to put it. Compacting is about volume, never about reachability.
   */
  compact?: boolean;
}

/** The coverage refusal on this decision, if any — the CTA into Attest keys on it. */
function coverageRefusal(decision: Decision): CoverageRefusal | null {
  const reason = decision.reasons.find((r) => r.code === 'card_coverage_incomplete');
  if (!reason) return null;
  const coverage = typeof reason.params?.coverage === 'string' ? reason.params.coverage : 'unattested';
  const unmatched = typeof reason.params?.unmatched === 'number' ? reason.params.unmatched : 0;
  return { coverage, unmatched };
}

export function DecisionFeedItem({ decision, onPress, onAttest, compact = false }: Props) {
  const swept = decision.action === 'sweep';
  const refusal = coverageRefusal(decision);

  // See `compact` above: a day with something to do keeps its card.
  const dense = compact && !refusal;

  // "Paid off" is a refusal carrying `no_debt` — never a third action. The backend derives
  // it once (`DayRecord.paid_off`) and every layer reads that flag rather than re-deriving
  // the rule, so the UI cannot drift into its own definition of what "done" means.
  const headline = decision.paid_off
    ? 'Card paid off'
    : swept
      ? `Paid ${formatMoney(decision.amount)}`
      : 'No payment today';

  const spine = swept ? colors.brandBlue : colors.deepGreen;
  const headlineColor = swept ? colors.blueText : colors.greenText;
  const reason = decision.reasons[0]?.text ?? '';

  const accessibilityLabel = `${headline} on ${formatDateShort(decision.date)}. Tap to explain.`;

  if (dense) {
    return (
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={accessibilityLabel}
        onPress={() => onPress(decision)}
        style={({ pressed }) => [styles.row, { borderLeftColor: spine }, pressed && styles.pressed]}
      >
        <Text style={styles.rowDate}>{formatDateShort(decision.date)}</Text>
        <Text style={[styles.rowHeadline, { color: headlineColor }]} numberOfLines={1}>
          {headline}
        </Text>
      </Pressable>
    );
  }

  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel}
      onPress={() => onPress(decision)}
      style={({ pressed }) => [styles.card, { borderLeftColor: spine }, pressed && styles.pressed]}
    >
      <View style={styles.header}>
        <Text style={styles.date}>{formatDateShort(decision.date)}</Text>
      </View>

      <Text style={[styles.headline, { color: headlineColor }]}>{headline}</Text>

      {reason !== '' && (
        <Text style={styles.reason} numberOfLines={2}>
          {reason}
        </Text>
      )}

      {/* The CTA into Attest — otherwise a user staring at "we can't confirm your cards" has no way
          to reach the screen that clears it (ticket 0052). Only on a coverage refusal, only when a
          handler is wired. The feed data stays read-only; this opens a screen, it does not write. */}
      {refusal && onAttest ? (
        <Pressable
          onPress={() => onAttest(refusal)}
          accessibilityRole="button"
          style={styles.cta}
          testID={`attest-cta-${decision.date}`}
        >
          <Text style={styles.ctaLabel}>Confirm your cards</Text>
        </Pressable>
      ) : null}
    </Pressable>
  );
}

/** The width of the outcome spine. Wide enough to read as a colour, narrow enough not to read as
 *  a second column. */
const SPINE = 4;

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.card,
    borderRadius: radius.card,
    borderLeftWidth: SPINE,
    padding: space.lg,
    marginBottom: space.md,
    minHeight: MIN_TAP_TARGET,
    ...shadow,
  },
  pressed: { opacity: 0.7 },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    marginBottom: space.sm,
  },
  date: { ...type.label },
  headline: { ...type.heading, marginBottom: space.xs },
  reason: { ...type.small },

  // The dense row. No shadow: a stack of eighty-seven lifted cards is the crowding this exists to
  // fix. White on the page's faint blue cast separates it perfectly well (see `theme.ts`), and the
  // 4px gap is what makes the run read as one list rather than one block.
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.md,
    backgroundColor: colors.card,
    borderRadius: radius.row,
    borderLeftWidth: SPINE,
    paddingHorizontal: space.md,
    minHeight: MIN_TAP_TARGET,
    marginBottom: space.xs,
  },
  // A fixed width, so the outcomes line up in a column instead of ragging off each date's length.
  rowDate: { ...type.label, width: 52 },
  rowHeadline: { ...type.small, fontWeight: '600', flex: 1 },

  cta: {
    marginTop: space.md,
    minHeight: MIN_TAP_TARGET,
    alignSelf: 'flex-start',
    justifyContent: 'center',
    paddingHorizontal: space.md,
    borderRadius: radius.pill,
    backgroundColor: colors.tabActive,
  },
  ctaLabel: { ...type.label, color: '#FFFFFF', fontSize: 14 },
});

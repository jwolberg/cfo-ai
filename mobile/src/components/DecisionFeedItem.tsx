/**
 * One decision, as one card.
 *
 * The reference cards do one idea each, and so does this: the outcome, the day, and the
 * engine's own first sentence about why. Everything else is behind the tap.
 *
 * The refusal is the case worth getting right. It is the most common outcome in the feed and
 * it is the product working — so it reads as a considered decision in the brand's deep green,
 * with a headline that says what happened to the money rather than what didn't happen. "No
 * payment today" is a statement; "Failed" or a red badge would be a lie about a system that
 * behaved exactly as designed.
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
}

/** The coverage refusal on this decision, if any — the CTA into Attest keys on it. */
function coverageRefusal(decision: Decision): CoverageRefusal | null {
  const reason = decision.reasons.find((r) => r.code === 'card_coverage_incomplete');
  if (!reason) return null;
  const coverage = typeof reason.params?.coverage === 'string' ? reason.params.coverage : 'unattested';
  const unmatched = typeof reason.params?.unmatched === 'number' ? reason.params.unmatched : 0;
  return { coverage, unmatched };
}

export function DecisionFeedItem({ decision, onPress, onAttest }: Props) {
  const swept = decision.action === 'sweep';
  const refusal = coverageRefusal(decision);

  // "Paid off" is a refusal carrying `no_debt` — never a third action. The backend derives
  // it once (`DayRecord.paid_off`) and every layer reads that flag rather than re-deriving
  // the rule, so the UI cannot drift into its own definition of what "done" means.
  const headline = decision.paid_off
    ? 'Card paid off'
    : swept
      ? `Paid ${formatMoney(decision.amount)}`
      : 'No payment today';

  const accent = swept ? colors.brandBlue : colors.deepGreen;
  const reason = decision.reasons[0]?.text ?? '';

  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`${headline} on ${formatDateShort(decision.date)}. Tap to explain.`}
      onPress={() => onPress(decision)}
      style={({ pressed }) => [styles.card, pressed && styles.pressed]}
    >
      <View style={styles.header}>
        {/* The accent bar from the reference cards — a divider, not a progress indicator. */}
        <View style={[styles.accent, { backgroundColor: accent }]} />
        <Text style={styles.date}>{formatDateShort(decision.date)}</Text>
      </View>

      <Text style={[styles.headline, { color: swept ? colors.blueText : colors.greenText }]}>
        {headline}
      </Text>

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

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.card,
    borderRadius: radius.card,
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
  accent: { width: 28, height: 4, borderRadius: 2 },
  date: { ...type.label },
  headline: { ...type.heading, marginBottom: space.xs },
  reason: { ...type.small },
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

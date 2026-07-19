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
  type NativeScrollEvent,
  type NativeSyntheticEvent,
  Pressable,
  StyleSheet,
  Text,
  View,
} from 'react-native';

import { ApiError, getDecisions, getLiveDecision } from '../api/client';
import type { Decision, DecisionsResponse, IsoDate } from '../api/types';
import { DecisionFeedItem, type CoverageRefusal } from '../components/DecisionFeedItem';
import { formatDateShort, formatMoney, formatMoneyRounded } from '../format';
import { COLUMN_WIDTH, MIN_TAP_TARGET, colors, radius, shadow, space, type } from '../theme';

type State =
  | { status: 'loading' }
  | { status: 'ready'; data: DecisionsResponse }
  // A *linked* household (ticket 0056): one live decision for `today`, no graded window.
  | { status: 'ready-live'; today: IsoDate; decision: Decision }
  | { status: 'failed'; kind: ApiError['kind'] };

interface Props {
  /** Which household's feed. Owned by `App.tsx` — see its note on why this is not local state. */
  householdId: string;
  /**
   * A *linked* household (`archetype === null`) is decided live from its current Plaid data through
   * `GET /live-decision` — one decision, no window. A seeded/demo household reads the graded feed
   * (`GET /decisions`). App.tsx knows which from the household's `archetype`, and the backend
   * enforces the same split (a demo household `409`s live-decision, a linked one `409`s /decisions).
   */
  linked?: boolean;
  onExplain: (decision: Decision) => void;
  /** Open Attest from a coverage-incomplete refusal in the feed (ticket 0052). */
  onAttest?: (refusal: CoverageRefusal) => void;
  /** Open the setup surface (Settings) from a linked household's "get started" status panel. */
  onGetStarted?: () => void;
}

/** The list's own top padding, which sits above the hero inside the scrolled content. */
const LIST_PADDING_TOP = space.xl;

/** Until the hero has measured itself, assume it is tall. Erring high means the summary bar
 *  appears a little late on the very first frame, rather than appearing *over* a hero that is
 *  still on screen — which is the one thing this is not allowed to do. */
const HERO_HEIGHT_FALLBACK = 320;

/** The dead zone between collapsing and expanding.
 *
 * A single threshold flickers the bar on and off on every pixel of scroll jitter, and a user
 * resting a thumb near it gets a strobe. So the bar appears once the hero is fully gone, and
 * does not leave again until you have scrolled back a little past that. (Classic hysteresis —
 * the same reason a thermostat doesn't cycle on every degree.)
 */
const HYSTERESIS = 24;

export function Dashboard({ householdId, linked, onExplain, onAttest, onGetStarted }: Props) {
  const [state, setState] = useState<State>({ status: 'loading' });
  const [collapsed, setCollapsed] = useState(false);

  // Where the hero's bottom edge sits in the scrolled content — **measured, not guessed.** The
  // summary may not appear until the hero it summarises is entirely off screen, and a hard-coded
  // pixel threshold is a claim about the hero's height that stops being true the first time
  // anyone edits it (a longer streak line, a wrapped hedge, a bigger font).
  const [heroEnd, setHeroEnd] = useState(LIST_PADDING_TOP + HERO_HEIGHT_FALLBACK);

  const onScroll = useCallback(
    (event: NativeSyntheticEvent<NativeScrollEvent>) => {
      const y = event.nativeEvent.contentOffset.y;
      setCollapsed((was) => (was ? y > heroEnd - HYSTERESIS : y >= heroEnd));
    },
    [heroEnd],
  );

  const onHeroLayout = useCallback((bottomWithinHeader: number) => {
    setHeroEnd(LIST_PADDING_TOP + bottomWithinHeader);
  }, []);

  const load = useCallback(async () => {
    // Back to `loading` first, on every household. The alternative is holding the previous
    // household's feed on screen while the next one is in flight, which is the stale-data bug
    // 0025 exists to avoid — and it is worse than a spinner precisely because it looks fine.
    setState({ status: 'loading' });
    try {
      if (linked) {
        // A linked household has no graded window — just today's live decision from its Plaid data.
        const live = await getLiveDecision(householdId);
        setState({ status: 'ready-live', today: live.today, decision: live.decision });
      } else {
        setState({ status: 'ready', data: await getDecisions(householdId) });
      }
    } catch (error) {
      // The timeout lives in the client, not here (`REQUEST_TIMEOUT_MS`), so there is no
      // path to a spinner that spins forever — the request always resolves one way or the
      // other, and both ways land in this state machine.
      const kind = error instanceof ApiError ? error.kind : 'network';
      setState({ status: 'failed', kind });
    }
  }, [householdId, linked]);

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

  if (state.status === 'ready-live') {
    return (
      <LiveDecision
        today={state.today}
        decision={state.decision}
        onExplain={onExplain}
        onAttest={onAttest}
        onGetStarted={onGetStarted}
      />
    );
  }

  const { summary, decisions } = state.data;

  return (
    <View style={styles.shell}>
      <FlatList
        testID="decision-feed"
        data={decisions}
        keyExtractor={(decision) => decision.date}
        contentContainerStyle={styles.list}
        style={styles.scroll}
        ListHeaderComponent={<Header summary={summary} onHeroLayout={onHeroLayout} />}
        ListEmptyComponent={<Empty />}
        renderItem={({ item }) => <DecisionFeedItem decision={item} onPress={onExplain} onAttest={onAttest} />}
        onScroll={onScroll}
        scrollEventThrottle={16}
      />

      {/* The hero, collapsed to the one thing worth keeping on screen: how far down the card is.
          Absolutely positioned *over* the list rather than pushing it, so the feed does not jump
          by the height of the bar the moment it appears. */}
      {collapsed && (
        <View style={styles.stickyLayer} pointerEvents="none">
          <View style={styles.stickyCard} testID="paydown-summary">
            <View style={styles.stickyInner}>
              <Paydown summary={summary} topless />
            </View>
          </View>
        </View>
      )}
    </View>
  );
}

/** How far the card has come down since the window opened, as a 0-1 fraction.
 *
 * The denominator is the card's balance on the **first served day**, which is why the backend
 * sends it. Clamped, because a card that grew — the household charged more than we paid off —
 * would otherwise render a negative bar, and this component is not the right place to argue
 * about that. It is the right place to refuse to draw it.
 */
function paidDownFraction(summary: DecisionsResponse['summary']): number {
  // `current`, not `targeted`. Both are the same figure for a one-card household, which is why
  // pairing `starting -> targeted` survived this long — but on a portfolio it measures a total
  // against a single card and draws progress nobody made.
  const start = Number(summary.starting_debt_balance);
  const now = Number(summary.current_debt_balance);
  if (!Number.isFinite(start) || start <= 0) return 0;
  return Math.min(1, Math.max(0, (start - now) / start));
}

/**
 * The paydown block: the one part of the hero that survives the scroll.
 *
 * Rendered by both the full hero and the collapsed summary bar, from the same props. Two copies
 * of "how far down is the card" would drift, and the day they drifted the sticky bar would be
 * quietly telling the user a different number from the card they just scrolled past.
 *
 * `topless` drops the top margin: inside the hero it needs air under the interest figure; alone
 * in the summary bar it is the only thing there.
 */
function Paydown({
  summary,
  topless = false,
}: {
  summary: DecisionsResponse['summary'];
  topless?: boolean;
}) {
  if (summary.paid_off) {
    // Deliberately *not* a `$0.00` — that reads like a bug on the one day it is unambiguously
    // good news. The panel that used to carry this is gone; the state it protected is not.
    return <Text style={[styles.paidOff, topless && styles.toplessPaidOff]}>Card paid off 🎉</Text>;
  }

  const percent = Math.round(paidDownFraction(summary) * 100);

  return (
    <View style={[styles.progressBlock, topless && styles.toplessBlock]}>
      <View style={styles.progressHead}>
        <Text style={styles.progressLabel}>Card paid down</Text>
        <Text style={styles.progressPercent}>{percent}%</Text>
      </View>
      {/* accessibility: a bar that only speaks in colour says nothing to a screen reader, and
          this is the number the whole screen is about. */}
      <View
        style={styles.track}
        accessibilityRole="progressbar"
        accessibilityValue={{ min: 0, max: 100, now: percent }}
      >
        <View style={[styles.fill, { width: `${percent}%` }]} />
      </View>
      <Text style={styles.progressFoot}>
        {formatMoneyRounded(summary.starting_debt_balance)} when we started →{' '}
        {formatMoneyRounded(summary.current_debt_balance)} now
      </Text>
    </View>
  );
}

function Header({
  summary,
  onHeroLayout,
}: {
  summary: DecisionsResponse['summary'];
  onHeroLayout: (bottomWithinHeader: number) => void;
}) {
  return (
    <View>
      <Text style={styles.greeting}>Your money, working.</Text>

      {/* The hero. Deep green, not white — this is the one card that is a reward rather than
          a readout, and it should not look like the stats beneath it.

          It reports its own bottom edge so the summary bar knows when it is fully out of sight.
          `layout.y` is relative to this header, and the list's top padding is added by the
          caller — together they are the exact scroll offset at which the hero disappears. */}
      <View
        style={styles.heroCard}
        testID="hero-card"
        onLayout={(event) => {
          const { y, height } = event.nativeEvent.layout;
          onHeroLayout(y + height);
        }}
      >
        {summary.sweep_count > 0 && (
          <Text style={styles.streak}>🔥 {summary.sweep_count}-payment streak</Text>
        )}

        <Text style={styles.heroLabel}>Beaten the bank out of</Text>
        <Text style={styles.heroValue}>{formatMoney(summary.interest_avoided_total)}</Text>
        {/* The engine's claim is conditional — `engine/interest.py` measures it against what
            they were *already* paying and hedges accordingly. The headline is allowed to
            celebrate; it is not allowed to drop the condition. */}
        <Text style={styles.heroFoot}>in interest, as long as you keep your payments up</Text>

        <Paydown summary={summary} />
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

/**
 * A linked household's live decision (ticket 0056). It has no graded *window*, so no
 * interest-avoided/streak rollup and no paydown bar — those summarise days of realized sweeps a
 * linked household hasn't had yet. In that hero's place sits a **status panel** that tells the
 * household where they stand instead of showing a hollow $0 reward: either "nothing owed" or "here's
 * what paying down early could do — get started". Below it, "today" and the single decision card
 * (which carries the Attest CTA on a coverage refusal, exactly as in the feed).
 *
 * The "why" tap (`onExplain`) has no live backend yet — `/decisions/{day}/explain` reads seeded rows
 * — so tapping opens the modal's own error state; a live explanation is a follow-up.
 */
function LiveDecision({
  today,
  decision,
  onExplain,
  onAttest,
  onGetStarted,
}: {
  today: IsoDate;
  decision: Decision;
  onExplain: (decision: Decision) => void;
  onAttest?: (refusal: CoverageRefusal) => void;
  /** Open the setup surface (Settings) from the "get started" CTA on the status panel. */
  onGetStarted?: () => void;
}) {
  return (
    <View style={styles.shell}>
      <View style={styles.list}>
        <Text style={styles.greeting}>Your money, working.</Text>
        <StatusHero decision={decision} onGetStarted={onGetStarted} />
        <Text style={styles.feedLabel}>Today · {formatDateShort(today)}</Text>
        <DecisionFeedItem decision={decision} onPress={onExplain} onAttest={onAttest} />
      </View>
    </View>
  );
}

/**
 * The status panel that stands in for the reward hero before a household has any realized paydown.
 * Two states, keyed on whether there is debt to pay down at all:
 *
 * - **Nothing owed** — no card is costing them interest. A win worth saying out loud, not a blank.
 * - **Debt, not yet working** — the opportunity, framed with their real balance, and a CTA into
 *   setup. This is the honest replacement for a "$0 beaten out of the bank" hero on a just-linked
 *   household: nothing has swept *yet*, so we point forward instead of celebrating a zero.
 *
 * `paid_off` and `debt_balance` come straight off the live decision — no summary needed.
 */
function StatusHero({
  decision,
  onGetStarted,
}: {
  decision: Decision;
  onGetStarted?: () => void;
}) {
  const owesInterest = !decision.paid_off && Number(decision.debt_balance) > 0;

  if (!owesInterest) {
    return (
      <View style={styles.heroCard}>
        <Text style={styles.heroLabel}>Your debt</Text>
        <Text style={styles.paidOff}>No interest to pay 🎉</Text>
        <Text style={styles.heroFoot}>
          Nothing is costing you interest right now. We&apos;ll keep watch and tell you if that
          changes.
        </Text>
      </View>
    );
  }

  return (
    <View style={styles.heroCard}>
      <Text style={styles.heroLabel}>You could be saving</Text>
      <Text style={styles.heroValue}>{formatMoney(decision.debt_balance)}</Text>
      <Text style={styles.heroFoot}>
        is sitting on high-interest cards. Move spare cash to the priciest one early and you pay less
        interest — automatically, once you&apos;re set up.
      </Text>
      {onGetStarted ? (
        <Pressable
          onPress={onGetStarted}
          accessibilityRole="button"
          style={({ pressed }) => [styles.heroCta, pressed && styles.pressed]}
          testID="get-started-cta"
        >
          <Text style={styles.heroCtaLabel}>Get started</Text>
        </Pressable>
      ) : null}
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
  shell: { flex: 1, backgroundColor: colors.page },
  scroll: { flex: 1, backgroundColor: colors.page },

  // The collapsed hero. Floats over the feed rather than pushing it down — a bar that reflows
  // the list the moment it appears makes the row under your thumb jump, which is how you tap
  // the wrong decision.
  //
  // **Full-bleed, and flush to the tab bar**: no top gap, no side margins, no corner radius.
  // It is a band pinned under the tabs, not a card floating on the page — which also means it
  // fully covers the feed rows passing behind it, instead of letting them peek out around a
  // rounded card.
  stickyLayer: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
  },
  stickyCard: {
    width: '100%',
    backgroundColor: colors.deepGreen,
    paddingHorizontal: space.lg,
    paddingVertical: space.md,
    ...shadow,
  },
  // The bar is full-bleed, but its contents still line up with the feed's column.
  stickyInner: {
    width: '100%',
    maxWidth: COLUMN_WIDTH,
    alignSelf: 'center',
  },
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

  // The subhead is gone, so the greeting carries the gap to the hero itself.
  greeting: { ...type.display, marginBottom: space.lg },

  // The reward card. Brand deep green, reversed out — the only inverted surface on the
  // screen, which is what makes it read as a prize and not a fourth statistic.
  heroCard: {
    backgroundColor: colors.deepGreen,
    borderRadius: radius.card,
    padding: space.lg,
    marginBottom: space.md,
    ...shadow,
  },
  streak: {
    ...type.label,
    color: colors.brandBlue,
    marginBottom: space.sm,
  },
  heroLabel: { ...type.label, color: '#8FBFB4' },
  heroValue: { fontSize: 38, fontWeight: '700', color: '#FFFFFF', marginTop: space.xs },
  heroFoot: { ...type.small, color: '#8FBFB4', marginTop: space.xs },

  // The "get started" CTA on the status panel. Blue on the deep-green hero — the one call to action,
  // set apart from the reversed-out text around it.
  heroCta: {
    marginTop: space.lg,
    minHeight: MIN_TAP_TARGET,
    alignSelf: 'flex-start',
    justifyContent: 'center',
    paddingHorizontal: space.lg,
    borderRadius: radius.pill,
    backgroundColor: colors.brandBlue,
  },
  heroCtaLabel: { color: '#FFFFFF', fontSize: 15, fontWeight: '600' },

  paidOff: { ...type.heading, color: '#FFFFFF', marginTop: space.lg },

  progressBlock: { marginTop: space.lg },
  // In the summary bar the paydown block is the only thing there, so it carries no top gap.
  toplessBlock: { marginTop: 0 },
  toplessPaidOff: { marginTop: 0 },
  progressHead: { flexDirection: 'row', justifyContent: 'space-between' },
  progressLabel: { ...type.label, color: '#8FBFB4' },
  progressPercent: { ...type.label, color: '#FFFFFF' },
  track: {
    height: 10,
    borderRadius: 5,
    // A visible trough, so the bar reads as "how far along" rather than a floating chip.
    backgroundColor: 'rgba(255,255,255,0.15)',
    marginTop: space.sm,
    overflow: 'hidden',
  },
  fill: { height: '100%', borderRadius: 5, backgroundColor: colors.leafGreen },
  progressFoot: { ...type.small, color: '#8FBFB4', marginTop: space.sm },

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

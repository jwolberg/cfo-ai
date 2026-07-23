/**
 * "Why?" — first for free, then with a model.
 *
 * Opening this costs no LLM call. The base narration is `engine/explain.py`, rendered
 * server-side and fetched over one endpoint — deterministic, tested, and incapable of
 * hallucinating. Only when the user types a follow-up does anything reach a model, and even
 * then the answer is checked against the engine's own records before it is shown
 * (`backend/assistant.py`). The most-read text in the product is the text that cannot be
 * wrong.
 *
 * ## Full-screen, and freshly mounted every time
 *
 * The modal covers the dashboard, so "switch to a different decision while it's open" is not
 * a reachable interaction and isn't built. Every open is a new mount against the tapped
 * decision, which means there is no conversation to carry over and no reset button to
 * remember to press — closing and reopening *is* the reset, by construction.
 *
 * Two ways out, both leading to the same place: the close control in the header, and Android's
 * hardware back (RN's `Modal` wires `onRequestClose` to it). A modal you can't leave is the
 * one dead end that would strand someone in a demo.
 */
import { useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  KeyboardAvoidingView,
  Modal,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';

import { ApiError, askAssistant, getExplanation } from '../api/client';
import type { BalanceProjection, Decision, Turn } from '../api/types';
import { BalanceChart } from '../components/BalanceChart';
import { formatDateLong, formatMoney } from '../format';
import { COLUMN_WIDTH, MIN_TAP_TARGET, colors, radius, shadow, space, type } from '../theme';

interface Props {
  decision: Decision | null;
  /** Whose decision this is. Owned by `App.tsx`; the modal never guesses.
   *
   *  Ticket 0025's sharpest acceptance criterion: asking "why not last Tuesday?" about household C
   *  must not answer about household A. Both the narration and the assistant carry this, so the
   *  backend loads *that* household's window and hands the model nothing else — the model cannot
   *  cite another household's figure, rather than being asked not to. */
  householdId: string;
  onClose: () => void;
}

type Narration =
  | { status: 'loading' }
  | { status: 'ready'; sentences: string[]; projection: BalanceProjection | null }
  | { status: 'failed' };

export function ExplainModal({ decision, householdId, onClose }: Props) {
  const [narration, setNarration] = useState<Narration>({ status: 'loading' });
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState('');
  const [thinking, setThinking] = useState(false);
  const scroller = useRef<ScrollView>(null);

  const date = decision?.date;

  useEffect(() => {
    if (!date) return;

    // A fresh mount per decision: the previous conversation is not carried over, because
    // there is no path back to it.
    setNarration({ status: 'loading' });
    setTurns([]);
    setDraft('');
    setThinking(false);

    let live = true;
    getExplanation(date, householdId)
      .then(
        (body) =>
          live &&
          setNarration({
            status: 'ready',
            sentences: body.narration,
            projection: body.projection,
          }),
      )
      .catch(() => live && setNarration({ status: 'failed' }));

    return () => {
      live = false;
    };
    // `householdId` is a dependency and not decoration: the same date exists in every household,
    // so a stale narration here would be a real, plausible, wrong answer rather than a blank.
  }, [date, householdId]);

  if (!decision) return null;

  async function send() {
    const question = draft.trim();
    if (question === '' || thinking) return;

    const history = turns;
    setTurns([...history, { role: 'user', content: question }]);
    setDraft('');
    setThinking(true);

    try {
      const { reply } = await askAssistant(question, history, householdId);
      setTurns((current) => [...current, { role: 'assistant', content: reply }]);
    } catch (error) {
      // A failed turn is an inline bubble, not a closed modal and not a fabricated answer.
      // The copy is deliberately about *us* being unreachable, never about the household's
      // money — a network error must never be mistaken for a fact about their finances.
      const timedOut = error instanceof ApiError && error.kind === 'timeout';
      setTurns((current) => [
        ...current,
        {
          role: 'assistant',
          content: timedOut
            ? "That took longer than it should have. Ask me again — your decisions haven't changed."
            : "I couldn't reach the assistant just then. Try asking again.",
        },
      ]);
    } finally {
      // Always re-enabled. An input that stays dead after a failure is a dead end.
      setThinking(false);
    }
  }

  const swept = decision.action === 'sweep';
  const headline = decision.paid_off
    ? 'Card paid off'
    : swept
      ? `Paid ${formatMoney(decision.amount)}`
      : 'No payment today';

  return (
    <Modal
      visible
      animationType="slide"
      presentationStyle="fullScreen"
      onRequestClose={onClose} // Android hardware back
    >
      <KeyboardAvoidingView
        style={styles.screen}
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
      >
        <View style={styles.header}>
          <View style={styles.headerText}>
            <Text style={styles.date}>{formatDateLong(decision.date)}</Text>
            <Text style={[styles.headline, { color: swept ? colors.blueText : colors.greenText }]}>
              {headline}
            </Text>
          </View>
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Close"
            onPress={onClose}
            style={({ pressed }) => [styles.close, pressed && styles.pressed]}
          >
            <Text style={styles.closeLabel}>✕</Text>
          </Pressable>
        </View>

        <ScrollView
          ref={scroller}
          style={styles.body}
          contentContainerStyle={styles.bodyContent}
          onContentSizeChange={() => scroller.current?.scrollToEnd({ animated: true })}
        >
          {narration.status === 'loading' && (
            <View style={styles.card} accessibilityRole="progressbar">
              <ActivityIndicator color={colors.brandBlue} />
            </View>
          )}

          {narration.status === 'failed' && (
            <View style={styles.card}>
              <Text style={styles.paragraph}>
                We couldn&apos;t load the explanation just now. Nothing has happened to your
                money — close this and try again.
              </Text>
            </View>
          )}

          {narration.status === 'ready' && (
            <View style={styles.card}>
              {narration.sentences.map((sentence) => (
                <Text key={sentence} style={styles.paragraph}>
                  {sentence}
                </Text>
              ))}
              {narration.projection !== null && (
                <BalanceChart projection={narration.projection} />
              )}
            </View>
          )}

          {turns.map((turn, index) => (
            <View
              key={`${turn.role}-${index}`}
              style={[styles.bubble, turn.role === 'user' ? styles.fromUser : styles.fromAgent]}
            >
              <Text style={turn.role === 'user' ? styles.userText : styles.agentText}>
                {turn.content}
              </Text>
            </View>
          ))}

          {thinking && (
            <View style={[styles.bubble, styles.fromAgent]}>
              {/* The backend may run several tool round-trips and wait on a model between
                  each. Without this the app looks frozen and the user sends the question
                  again — which costs a second turn and answers neither. */}
              <Text style={styles.agentText}>Looking it up…</Text>
            </View>
          )}
        </ScrollView>

        <View style={styles.composer}>
          <TextInput
            style={styles.input}
            value={draft}
            onChangeText={setDraft}
            editable={!thinking}
            placeholder="Ask why — “what about last Tuesday?”"
            placeholderTextColor={colors.muted}
            accessibilityLabel="Ask a follow-up question"
            returnKeyType="send"
            onSubmitEditing={send}
          />
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Send"
            disabled={thinking || draft.trim() === ''}
            onPress={send}
            style={({ pressed }) => [
              styles.send,
              (thinking || draft.trim() === '') && styles.sendDisabled,
              pressed && styles.pressed,
            ]}
          >
            <Text style={styles.sendLabel}>Ask</Text>
          </Pressable>
        </View>
      </KeyboardAvoidingView>
    </Modal>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.page },
  header: {
    flexDirection: 'row',
    alignItems: 'flex-start',
    paddingTop: space.xl + space.lg,
    paddingHorizontal: space.lg,
    paddingBottom: space.md,
  },
  headerText: { flex: 1 },
  date: { ...type.label },
  headline: { ...type.heading, marginTop: space.xs },
  close: {
    width: MIN_TAP_TARGET,
    height: MIN_TAP_TARGET,
    alignItems: 'center',
    justifyContent: 'center',
    borderRadius: radius.pill,
  },
  closeLabel: { fontSize: 20, color: colors.body },

  body: { flex: 1 },
  bodyContent: {
    paddingHorizontal: space.lg,
    paddingBottom: space.lg,
    width: '100%',
    maxWidth: COLUMN_WIDTH,
    alignSelf: 'center',
  },
  card: {
    backgroundColor: colors.card,
    borderRadius: radius.card,
    padding: space.lg,
    marginBottom: space.md,
    ...shadow,
  },
  paragraph: { ...type.body, marginBottom: space.sm },

  bubble: {
    borderRadius: radius.card,
    padding: space.md,
    marginBottom: space.sm,
    maxWidth: '88%',
  },
  fromUser: { alignSelf: 'flex-end', backgroundColor: colors.brandBlue },
  fromAgent: { alignSelf: 'flex-start', backgroundColor: colors.card, ...shadow },
  userText: { color: '#FFFFFF', fontSize: 16, lineHeight: 22 },
  agentText: { ...type.body },

  composer: {
    flexDirection: 'row',
    gap: space.sm,
    padding: space.md,
    borderTopWidth: 1,
    borderTopColor: colors.border,
    backgroundColor: colors.card,
    width: '100%',
    maxWidth: COLUMN_WIDTH,
    alignSelf: 'center',
  },
  input: {
    flex: 1,
    minHeight: MIN_TAP_TARGET,
    borderRadius: radius.pill,
    paddingHorizontal: space.md,
    backgroundColor: colors.page,
    color: colors.ink,
    fontSize: 16,
  },
  send: {
    minHeight: MIN_TAP_TARGET,
    paddingHorizontal: space.lg,
    borderRadius: radius.pill,
    backgroundColor: colors.brandBlue,
    alignItems: 'center',
    justifyContent: 'center',
  },
  sendDisabled: { opacity: 0.4 },
  sendLabel: { color: '#FFFFFF', fontSize: 16, fontWeight: '600' },
  pressed: { opacity: 0.8 },
});

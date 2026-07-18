/**
 * Attest — "confirm your cards" (ticket 0052, U6b), over `POST /attest`.
 *
 * The engine refuses to sweep a household whose card set it cannot be sure is complete. This screen
 * is how a user clears that gate — but only when it *can* be cleared. Two distinct states, because
 * they are two different situations and telling them apart is the point:
 *
 * - **Unattested** — we have the cards, the user just hasn't confirmed the list is complete. One
 *   button confirms it.
 * - **Unmatched payment** — we can see a payment to a card we *cannot* see, so attesting would be a
 *   lie the engine would act on. This is a **dead end this rung**: the screen names the blocking
 *   card, says there is no in-app fix yet (the Link UI is the next rung), and points to support —
 *   rather than offering a button that silently fails.
 *
 * The coverage state and the unmatched count arrive from the feed item that opened this screen, so
 * no extra fetch is needed to know which state to show.
 */
import { useEffect, useState } from 'react';
import { Modal, Pressable, StyleSheet, Text, View } from 'react-native';

import { ApiError, attest } from '../api/client';
import { COLUMN_WIDTH, MIN_TAP_TARGET, colors, radius, space, type } from '../theme';

/** The coverage states the engine reports (`engine/models.py:CoverageState`). `complete` never
 *  opens this screen; the feed CTA only appears while coverage is incomplete. */
export type Coverage = 'unattested' | 'unmatched_payment' | 'complete';

interface Props {
  visible: boolean;
  householdId: string;
  /** From the feed item's `card_coverage_incomplete` reason params. */
  coverage: Coverage;
  unmatched: number;
  onClose: () => void;
  /** So the parent can refresh the feed after a successful attestation. */
  onAttested?: () => void;
}

type Status = 'idle' | 'attesting' | 'done' | 'error' | 'not_allowed';

export function Attest({ visible, householdId, coverage, unmatched, onClose, onAttested }: Props) {
  const [status, setStatus] = useState<Status>('idle');

  useEffect(() => {
    if (visible) setStatus('idle');
  }, [visible]);

  async function confirm() {
    setStatus('attesting');
    try {
      await attest(householdId);
      setStatus('done');
      onAttested?.();
    } catch (error) {
      setStatus(error instanceof ApiError && error.kind === 'unauthorized' ? 'not_allowed' : 'error');
    }
  }

  const blocked = coverage === 'unmatched_payment';

  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose}>
      <View style={styles.scrim} testID="attest-modal">
        <View style={styles.sheet}>
          <View style={styles.header}>
            <Text style={styles.title}>Confirm your cards</Text>
            <Pressable onPress={onClose} accessibilityRole="button" testID="attest-close">
              <Text style={styles.close}>Done</Text>
            </Pressable>
          </View>

          {blocked ? (
            <View testID="attest-blocked">
              {/* The dead end, named — not a silent write failure. */}
              <Text style={styles.body}>
                We can see {unmatched === 1 ? 'a payment' : 'payments'} to a card we don&rsquo;t have
                on file. Until that card is connected, we can&rsquo;t confirm your list is complete,
                so sweeps stay paused for your safety.
              </Text>
              <Text style={styles.bodyMuted}>
                There isn&rsquo;t a way to fix this in the app yet. Connecting the missing card is
                coming soon; in the meantime, reach out to support and we&rsquo;ll help.
              </Text>
            </View>
          ) : status === 'done' ? (
            <Text style={styles.body} testID="attest-done">
              Thanks — your cards are confirmed.
            </Text>
          ) : (
            <View testID="attest-confirm">
              <Text style={styles.body}>
                Are these all of your credit cards? Confirming lets us keep every card&rsquo;s bill
                covered before a sweep.
              </Text>
              {status === 'error' ? (
                <Text style={styles.error} testID="attest-error">
                  Something went wrong. Please try again.
                </Text>
              ) : null}
              {status === 'not_allowed' ? (
                <Text style={styles.error} testID="attest-not-allowed">
                  You don&rsquo;t have permission to confirm cards for this household.
                </Text>
              ) : null}
              <Pressable
                onPress={confirm}
                disabled={status === 'attesting'}
                accessibilityRole="button"
                accessibilityState={{ disabled: status === 'attesting' }}
                style={[styles.primary, status === 'attesting' ? styles.primaryDisabled : null]}
                testID="attest-confirm-button"
              >
                <Text style={styles.primaryLabel}>
                  {status === 'attesting' ? 'Confirming…' : 'Yes, these are all my cards'}
                </Text>
              </Pressable>
            </View>
          )}
        </View>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  scrim: { flex: 1, backgroundColor: 'rgba(0,0,0,0.35)', justifyContent: 'flex-end' },
  sheet: {
    width: '100%',
    maxWidth: COLUMN_WIDTH,
    alignSelf: 'center',
    backgroundColor: colors.page,
    borderTopLeftRadius: radius.card,
    borderTopRightRadius: radius.card,
    padding: space.lg,
    gap: space.md,
  },
  header: { flexDirection: 'row', alignItems: 'center' },
  title: { ...type.heading, flex: 1 },
  close: { ...type.label, color: colors.tabActive },
  body: { ...type.body },
  bodyMuted: { ...type.small, color: colors.muted },
  error: { ...type.small, color: '#C0392B' },
  primary: {
    minHeight: MIN_TAP_TARGET,
    backgroundColor: colors.tabActive,
    borderRadius: radius.pill,
    alignItems: 'center',
    justifyContent: 'center',
    marginTop: space.md,
    paddingHorizontal: space.lg,
  },
  primaryDisabled: { opacity: 0.6 },
  primaryLabel: { ...type.label, color: '#FFFFFF', fontSize: 15 },
});

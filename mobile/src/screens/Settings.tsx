/**
 * Settings — the product's first user-facing write screen (ticket 0052, U6b), over `PATCH /policy`.
 *
 * The guardrails are safety-critical, so the interaction states are specified, not left to chance:
 * no silent no-ops. Every write shows a **saving** state, ends in an **explicit success**, and a
 * rejected change surfaces its reason **inline** rather than vanishing — a guardrail edit the user
 * cannot confirm is itself a safety problem. Validation mirrors the backend's bounds (U4) so the
 * common mistakes are caught before a round-trip; the server's 422 is still surfaced as the backstop.
 *
 * **Pause is UI over `blackout_dates`.** Enabling a pause takes a lightweight confirm (it stops money
 * movement); active pauses are listed and each is individually removable to un-pause. The screen
 * stays deliberately minimal — this is where scope balloons.
 */
import { useEffect, useState } from 'react';
import {
  ActivityIndicator,
  Modal,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';

import { ApiError, getPolicy, updatePolicy } from '../api/client';
import type { Policy } from '../api/types';
import { COLUMN_WIDTH, MIN_TAP_TARGET, colors, radius, space, type } from '../theme';

interface Props {
  visible: boolean;
  householdId: string;
  onClose: () => void;
}

// The engine's minimum sweep (`engine/decide.py:MIN_SWEEP`). Mirrored here so a below-minimum cap is
// caught inline; the backend enforces it regardless (ticket 0049).
const MIN_SWEEP = 1;

type Form = {
  buffer_floor: string;
  max_sweep: string;
  max_weekly_sweep: string;
  min_days_between_sweeps: string;
  blackout_dates: string[];
};

type Status = 'loading' | 'ready' | 'saving' | 'saved' | 'load_failed';

function toForm(p: Policy): Form {
  return {
    buffer_floor: p.buffer_floor,
    max_sweep: p.max_sweep,
    max_weekly_sweep: p.max_weekly_sweep,
    min_days_between_sweeps: String(p.min_days_between_sweeps),
    blackout_dates: [...p.blackout_dates],
  };
}

/** Client-side mirror of U4's bounds → per-field messages. Empty object means valid. */
function validate(f: Form): Partial<Record<keyof Form, string>> {
  const errors: Partial<Record<keyof Form, string>> = {};
  const floor = Number(f.buffer_floor);
  const sweep = Number(f.max_sweep);
  const weekly = Number(f.max_weekly_sweep);
  const days = Number(f.min_days_between_sweeps);

  if (f.buffer_floor.trim() === '' || Number.isNaN(floor) || floor < 0) {
    errors.buffer_floor = 'Enter an amount of $0 or more.';
  }
  if (f.max_sweep.trim() === '' || Number.isNaN(sweep) || sweep < MIN_SWEEP) {
    errors.max_sweep = `Enter at least $${MIN_SWEEP.toFixed(2)}.`;
  }
  if (f.max_weekly_sweep.trim() === '' || Number.isNaN(weekly) || weekly < sweep) {
    errors.max_weekly_sweep = 'The weekly cap cannot be below a single sweep.';
  }
  if (!Number.isInteger(days) || days < 0 || days > 90) {
    errors.min_days_between_sweeps = 'Enter a whole number of days from 0 to 90.';
  }
  return errors;
}

export function Settings({ visible, householdId, onClose }: Props) {
  const [status, setStatus] = useState<Status>('loading');
  const [form, setForm] = useState<Form | null>(null);
  const [errors, setErrors] = useState<Partial<Record<keyof Form, string>>>({});
  const [serverErrors, setServerErrors] = useState<string[]>([]);
  const [notAllowed, setNotAllowed] = useState(false);
  const [pauseDraft, setPauseDraft] = useState('');
  const [confirmPause, setConfirmPause] = useState<string | null>(null);

  useEffect(() => {
    if (!visible) return;
    setStatus('loading');
    setErrors({});
    setServerErrors([]);
    setNotAllowed(false);
    setConfirmPause(null);
    getPolicy(householdId)
      .then((p) => {
        setForm(toForm(p));
        setStatus('ready');
      })
      .catch(() => setStatus('load_failed'));
  }, [visible, householdId]);

  function set<K extends keyof Form>(key: K, value: Form[K]) {
    setForm((f) => (f ? { ...f, [key]: value } : f));
    setStatus('ready'); // editing after a save clears the "Saved" confirmation
  }

  async function save() {
    if (!form) return;
    const found = validate(form);
    setErrors(found);
    setServerErrors([]);
    setNotAllowed(false);
    if (Object.keys(found).length > 0) return; // no write on an invalid edit

    setStatus('saving');
    try {
      await updatePolicy(householdId, {
        buffer_floor: form.buffer_floor,
        max_sweep: form.max_sweep,
        max_weekly_sweep: form.max_weekly_sweep,
        min_days_between_sweeps: Number(form.min_days_between_sweeps),
        blackout_dates: form.blackout_dates,
      });
      setStatus('saved');
    } catch (error) {
      setStatus('ready');
      if (error instanceof ApiError && error.kind === 'validation') {
        setServerErrors(Array.isArray(error.detail) ? (error.detail as string[]) : ['Rejected.']);
      } else if (error instanceof ApiError && error.kind === 'unauthorized') {
        setNotAllowed(true);
      } else {
        setServerErrors(['Could not save. Please try again.']);
      }
    }
  }

  function addBlackout(date: string) {
    setForm((f) => (f && !f.blackout_dates.includes(date) ? { ...f, blackout_dates: [...f.blackout_dates, date] } : f));
    setConfirmPause(null);
    setPauseDraft('');
    setStatus('ready');
  }

  function removeBlackout(date: string) {
    setForm((f) => (f ? { ...f, blackout_dates: f.blackout_dates.filter((d) => d !== date) } : f));
    setStatus('ready');
  }

  const today = new Date().toISOString().slice(0, 10);

  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose}>
      <View style={styles.scrim} testID="settings-modal">
        <View style={styles.sheet}>
          <View style={styles.header}>
            <Text style={styles.title}>Settings</Text>
            <Pressable onPress={onClose} accessibilityRole="button" testID="settings-close">
              <Text style={styles.close}>Done</Text>
            </Pressable>
          </View>

          {status === 'loading' ? (
            <ActivityIndicator color={colors.tabActive} />
          ) : status === 'load_failed' || !form ? (
            <Text style={styles.body}>We couldn&rsquo;t load your settings. Please try again.</Text>
          ) : (
            <ScrollView>
              <Field
                label="Buffer floor"
                hint="The cash we never touch."
                value={form.buffer_floor}
                onChangeText={(v) => set('buffer_floor', v)}
                error={errors.buffer_floor}
                keyboardType="decimal-pad"
                testID="field-buffer_floor"
              />
              <Field
                label="Max single sweep"
                value={form.max_sweep}
                onChangeText={(v) => set('max_sweep', v)}
                error={errors.max_sweep}
                keyboardType="decimal-pad"
                testID="field-max_sweep"
              />
              <Field
                label="Max weekly sweep"
                value={form.max_weekly_sweep}
                onChangeText={(v) => set('max_weekly_sweep', v)}
                error={errors.max_weekly_sweep}
                keyboardType="decimal-pad"
                testID="field-max_weekly_sweep"
              />
              <Field
                label="Days between sweeps"
                value={form.min_days_between_sweeps}
                onChangeText={(v) => set('min_days_between_sweeps', v)}
                error={errors.min_days_between_sweeps}
                keyboardType="number-pad"
                testID="field-min_days_between_sweeps"
              />

              {/* Pause — UI over blackout_dates. */}
              <Text style={styles.sectionTitle}>Pause</Text>
              <Text style={styles.hint}>On a paused day no money moves.</Text>
              {form.blackout_dates.length === 0 ? (
                <Text style={styles.hint}>No paused days.</Text>
              ) : (
                form.blackout_dates.map((d) => (
                  <View key={d} style={styles.pauseRow} testID={`pause-${d}`}>
                    <Text style={styles.body}>{d}</Text>
                    <Pressable
                      onPress={() => removeBlackout(d)}
                      accessibilityRole="button"
                      testID={`pause-remove-${d}`}
                    >
                      <Text style={styles.remove}>Remove</Text>
                    </Pressable>
                  </View>
                ))
              )}

              {confirmPause === null ? (
                <Pressable
                  onPress={() => setConfirmPause(pauseDraft.trim() || today)}
                  accessibilityRole="button"
                  style={styles.secondary}
                  testID="pause-today"
                >
                  <Text style={styles.secondaryLabel}>Pause today</Text>
                </Pressable>
              ) : (
                <View style={styles.confirmBox} testID="pause-confirm">
                  <Text style={styles.body}>Pause money movement on {confirmPause}?</Text>
                  <View style={styles.confirmRow}>
                    <Pressable
                      onPress={() => addBlackout(confirmPause)}
                      accessibilityRole="button"
                      style={styles.primary}
                      testID="pause-confirm-yes"
                    >
                      <Text style={styles.primaryLabel}>Confirm pause</Text>
                    </Pressable>
                    <Pressable
                      onPress={() => setConfirmPause(null)}
                      accessibilityRole="button"
                      testID="pause-confirm-no"
                    >
                      <Text style={styles.remove}>Cancel</Text>
                    </Pressable>
                  </View>
                </View>
              )}

              {serverErrors.length > 0 ? (
                <View testID="settings-error">
                  {serverErrors.map((e) => (
                    <Text key={e} style={styles.error}>
                      {e}
                    </Text>
                  ))}
                </View>
              ) : null}
              {notAllowed ? (
                <Text style={styles.error} testID="settings-not-allowed">
                  You don&rsquo;t have permission to change these settings.
                </Text>
              ) : null}
              {status === 'saved' ? (
                <Text style={styles.saved} testID="settings-saved">
                  Saved.
                </Text>
              ) : null}

              <Pressable
                onPress={save}
                disabled={status === 'saving'}
                accessibilityRole="button"
                accessibilityState={{ disabled: status === 'saving' }}
                style={[styles.primary, status === 'saving' ? styles.primaryDisabled : null]}
                testID="settings-save"
              >
                <Text style={styles.primaryLabel}>{status === 'saving' ? 'Saving…' : 'Save'}</Text>
              </Pressable>
            </ScrollView>
          )}
        </View>
      </View>
    </Modal>
  );
}

function Field({
  label,
  hint,
  value,
  onChangeText,
  error,
  keyboardType,
  testID,
}: {
  label: string;
  hint?: string;
  value: string;
  onChangeText: (v: string) => void;
  error?: string;
  keyboardType: 'decimal-pad' | 'number-pad';
  testID: string;
}) {
  return (
    <View style={styles.field}>
      <Text style={styles.label}>{label}</Text>
      {hint ? <Text style={styles.hint}>{hint}</Text> : null}
      <TextInput
        value={value}
        onChangeText={onChangeText}
        keyboardType={keyboardType}
        style={[styles.input, error ? styles.inputError : null]}
        testID={testID}
        accessibilityLabel={label}
      />
      {error ? (
        <Text style={styles.error} testID={`${testID}-error`}>
          {error}
        </Text>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  scrim: { flex: 1, backgroundColor: 'rgba(0,0,0,0.35)', justifyContent: 'flex-end' },
  sheet: {
    width: '100%',
    maxWidth: COLUMN_WIDTH,
    alignSelf: 'center',
    maxHeight: '90%',
    backgroundColor: colors.page,
    borderTopLeftRadius: radius.card,
    borderTopRightRadius: radius.card,
    padding: space.lg,
  },
  header: { flexDirection: 'row', alignItems: 'center', marginBottom: space.md },
  title: { ...type.heading, flex: 1 },
  close: { ...type.label, color: colors.tabActive },
  sectionTitle: { ...type.heading, fontSize: 16, marginTop: space.lg },
  field: { marginBottom: space.md },
  label: { ...type.label, color: colors.ink },
  hint: { ...type.small, color: colors.muted },
  input: {
    minHeight: MIN_TAP_TARGET,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.border,
    borderRadius: radius.pill,
    paddingHorizontal: space.md,
    backgroundColor: colors.card,
    ...type.body,
    color: colors.ink,
  },
  inputError: { borderColor: '#C0392B' },
  error: { ...type.small, color: '#C0392B', marginTop: space.xs },
  saved: { ...type.body, color: '#1E8449', marginTop: space.sm },
  body: { ...type.body },
  remove: { ...type.label, color: '#C0392B' },
  pauseRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingVertical: space.sm,
  },
  secondary: {
    minHeight: MIN_TAP_TARGET,
    justifyContent: 'center',
    marginTop: space.sm,
  },
  secondaryLabel: { ...type.label, color: colors.tabActive },
  confirmBox: {
    marginTop: space.sm,
    padding: space.md,
    backgroundColor: colors.card,
    borderRadius: radius.card,
    gap: space.sm,
  },
  confirmRow: { flexDirection: 'row', alignItems: 'center', gap: space.lg },
  primary: {
    minHeight: MIN_TAP_TARGET,
    backgroundColor: colors.tabActive,
    borderRadius: radius.pill,
    alignItems: 'center',
    justifyContent: 'center',
    marginTop: space.lg,
    paddingHorizontal: space.lg,
  },
  primaryDisabled: { opacity: 0.6 },
  primaryLabel: { ...type.label, color: '#FFFFFF', fontSize: 15 },
});

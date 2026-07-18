/**
 * Which household the demo is showing. Ticket 0025.
 *
 * ## This is not a customer feature, and the design says so
 *
 * `USERS.md` names two audiences and says they "should not be conflated". The customer has **one**
 * household — it is theirs, and a picker would be nonsense on their surface. The reviewer wants to
 * see, in under a minute, what the engine does to households that are not the demo's. This is
 * entirely for the second one.
 *
 * So it sits *lightly*: a single line above the tabs, the same height as a row of chrome, styled
 * like an account picker rather than a console. `USERS.md` §2 is explicit that "the demonstration
 * **is** the product surface; a second, instrumented 'admin' view would undercut the point" — so
 * there is no admin screen, no comparison grid, no dashboard-of-dashboards. If this ever grows one,
 * it has gone wrong.
 *
 * ## Hand-rolled, for the reason `App.tsx` gives about the router
 *
 * Four options and no nesting. A `Modal` and a column of `Pressable`s say exactly what is
 * happening; a picker library is a great deal of machinery for a list of four strings.
 *
 * ## The labels are the whole point
 *
 * They come from the backend (`GET /households`) and name what makes each household *different* —
 * pay cadence and card count. "Household B" would be a failure: the reason these four exist is
 * comparison, and a label you cannot compare with is a label that has not done its job.
 */
import { useState } from 'react';
import { Modal, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';

import type { Household } from '../api/types';
import { COLUMN_WIDTH, MIN_TAP_TARGET, colors, radius, space, type } from '../theme';

interface Props {
  /**
   * The households this session may switch to — the caller's memberships, owned and fetched by
   * `App` (ticket 0051). The picker used to fetch its own list; now `App` does, once, because it
   * also needs the list to choose the initial household and to decide whether the picker renders at
   * all (a single-membership user has no choice to make). Presentational from here.
   */
  households: Household[];
  selected: string;
  onSelect: (householdId: string) => void;
}

export function HouseholdPicker({ households, selected, onSelect }: Props) {
  const [open, setOpen] = useState(false);

  const current = households.find((h) => h.id === selected);

  return (
    <View style={styles.bar}>
      <Pressable
        onPress={() => setOpen(true)}
        style={styles.trigger}
        accessibilityRole="button"
        accessibilityLabel={`Household: ${current?.label ?? selected}. Tap to switch.`}
        testID="household-picker"
      >
        <Text style={styles.eyebrow}>Household</Text>
        <Text style={styles.current} numberOfLines={1}>
          {current?.label ?? selected}
        </Text>
        {/* A caret, not an icon font. One glyph is not a dependency. */}
        <Text style={styles.caret}>▾</Text>
      </Pressable>

      <Modal
        visible={open}
        transparent
        animationType="fade"
        onRequestClose={() => setOpen(false)}
      >
        {/* Tapping outside closes. A picker you can only leave by choosing is a trap. */}
        <Pressable style={styles.scrim} onPress={() => setOpen(false)}>
          <View style={styles.sheet}>
            <Text style={styles.sheetTitle}>Show me</Text>
            <ScrollView>
              {households.map((h) => (
                <Option
                  key={h.id}
                  household={h}
                  active={h.id === selected}
                  onPress={() => {
                    onSelect(h.id);
                    setOpen(false);
                  }}
                />
              ))}
            </ScrollView>
          </View>
        </Pressable>
      </Modal>
    </View>
  );
}

function Option({
  household,
  active,
  onPress,
}: {
  household: Household;
  active: boolean;
  onPress: () => void;
}) {
  return (
    <Pressable
      onPress={onPress}
      style={[styles.option, active ? styles.optionActive : null]}
      accessibilityRole="button"
      accessibilityState={{ selected: active }}
      testID={`household-option-${household.id}`}
    >
      <Text style={[styles.optionLabel, active ? styles.optionLabelActive : null]}>
        {household.label}
      </Text>
      {active ? <Text style={styles.tick}>✓</Text> : null}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  bar: {
    backgroundColor: colors.card,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: colors.border,
    alignItems: 'center',
  },
  trigger: {
    width: '100%',
    maxWidth: COLUMN_WIDTH,
    minHeight: MIN_TAP_TARGET,
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    paddingHorizontal: space.lg,
    paddingVertical: space.sm,
  },
  eyebrow: { ...type.label, color: colors.muted },
  current: { ...type.label, fontSize: 15, color: colors.ink, flexShrink: 1 },
  caret: { ...type.label, color: colors.muted, marginLeft: 'auto' },
  scrim: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.35)',
    justifyContent: 'center',
    alignItems: 'center',
    padding: space.lg,
  },
  sheet: {
    width: '100%',
    maxWidth: COLUMN_WIDTH,
    backgroundColor: colors.card,
    borderRadius: radius.card,
    padding: space.sm,
    maxHeight: '70%',
  },
  sheetTitle: {
    ...type.label,
    color: colors.muted,
    paddingHorizontal: space.md,
    paddingTop: space.sm,
    paddingBottom: space.xs,
  },
  option: {
    minHeight: MIN_TAP_TARGET,
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
    borderRadius: radius.pill,
  },
  optionActive: { backgroundColor: colors.tabActive },
  optionLabel: { ...type.body, color: colors.ink, flexShrink: 1 },
  optionLabelActive: { color: '#FFFFFF' },
  tick: { ...type.body, color: '#FFFFFF', marginLeft: 'auto' },
});

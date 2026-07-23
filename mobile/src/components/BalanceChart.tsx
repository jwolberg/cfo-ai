/**
 * The projected-balance chart (ticket 0064): the forecast every decision turns on, drawn.
 *
 * The narration already says it in words — "your cash bottoms out at $1,291.47 on June 5, after
 * your $800 buffer." This is that same sentence as a curve. The data comes from the explain
 * response, derived server-side from the frozen snapshot on the *same walk* the decision used, so
 * the chart's lowest point **is** the number the narration quotes; they cannot disagree.
 *
 * ## Tone is the whole job
 *
 * The projection is the conservative worst case (the forecast resolves every uncertainty toward the
 * outcome that hurts). So this is drawn to read *"the line stays above my floor"*, never *"look how
 * low it dips"* — no red (`theme.ts` has none on purpose), a quiet cushion fill, and a caption that,
 * on a tight-cash day where the low falls **below** the floor, names the dip as the reason we held
 * back rather than an alarm. A household being shown the truth is being reassured, not warned.
 */
import { StyleSheet, Text, View } from 'react-native';
import Svg, { Circle, Line, Polygon, Polyline } from 'react-native-svg';

import type { BalanceProjection } from '../api/types';
import { formatDateShort, formatMoney } from '../format';
import { colors, space, type } from '../theme';

// A fixed coordinate system the SVG scales to the card width via `viewBox` — no layout measurement,
// same trick the hand-rolled StripChart avoids a dependency with, but this one earns `react-native-svg`.
const W = 320;
const H = 128;
const PAD_X = 6;
const PAD_TOP = 12;
const PAD_BOTTOM = 12;

export function BalanceChart({ projection }: { projection: BalanceProjection }) {
  const points = projection.points;
  if (points.length < 2) return null;

  const values = points.map((p) => Number.parseFloat(p.balance));
  const floor = Number.parseFloat(projection.buffer_floor);
  const low = Number.parseFloat(projection.low);
  const belowFloor = low < floor;

  // The floor is always in view, so a household can see the line sit above it (or, honestly, dip
  // below it). Domain runs from the lower of the low and the floor to the highest projected point.
  const yMin = Math.min(low, floor);
  const yMax = Math.max(...values, floor);
  const span = Math.max(1, yMax - yMin);

  const px = (i: number) => PAD_X + (i / (points.length - 1)) * (W - 2 * PAD_X);
  const py = (v: number) => PAD_TOP + (1 - (v - yMin) / span) * (H - PAD_TOP - PAD_BOTTOM);

  const line = values.map((v, i) => `${px(i)},${py(v)}`).join(' ');
  const baseY = H - PAD_BOTTOM;
  const area = `${px(0)},${baseY} ${line} ${px(points.length - 1)},${baseY}`;
  const lowIndex = points.findIndex((p) => p.day === projection.low_day);

  return (
    <View style={styles.wrap} testID="balance-chart">
      <Text style={styles.title}>WHERE YOUR CASH IS HEADED</Text>

      <Svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
        {/* `none` fills the card width; height matches the viewBox so only x stretches — the
            y (dollars) keeps its 1:1 scale, no distortion. */}
        <Polygon points={area} fill={colors.leafGreen} fillOpacity={0.1} />
        <Line
          x1={PAD_X}
          y1={py(floor)}
          x2={W - PAD_X}
          y2={py(floor)}
          stroke={colors.muted}
          strokeWidth={1}
          strokeDasharray="4,3"
        />
        <Polyline
          points={line}
          fill="none"
          stroke={colors.brandBlue}
          strokeWidth={2}
          strokeLinejoin="round"
          strokeLinecap="round"
        />
        {lowIndex >= 0 && (
          <Circle
            cx={px(lowIndex)}
            cy={py(low)}
            r={4}
            fill={colors.brandBlue}
            stroke={colors.card}
            strokeWidth={1.5}
          />
        )}
      </Svg>

      <View style={styles.legend}>
        <View style={styles.legendItem}>
          <View style={styles.floorSwatch} />
          <Text style={styles.legendText}>{formatMoney(projection.buffer_floor)} buffer</Text>
        </View>
        <View style={styles.legendItem}>
          <View style={styles.lowDot} />
          <Text style={styles.legendText}>
            Low {formatMoney(projection.low)} on {formatDateShort(projection.low_day)}
          </Text>
        </View>
      </View>

      <View style={styles.axis}>
        <Text style={styles.axisText}>today</Text>
        <Text style={styles.axisText}>{formatDateShort(projection.horizon_end)}</Text>
      </View>

      <Text style={styles.caption}>
        {belowFloor
          ? `Your cash gets tight around ${formatDateShort(projection.low_day)}, so we're leaving it alone.`
          : `The lowest we expect over the next 30 days, worst case — after your ${formatMoney(
              projection.buffer_floor,
            )} buffer. We plan against the bad version so the good one takes care of itself.`}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { marginTop: space.md },
  title: { ...type.label, marginBottom: space.xs, letterSpacing: 0.6 },
  legend: { flexDirection: 'row', flexWrap: 'wrap', gap: space.md, marginTop: space.xs },
  legendItem: { flexDirection: 'row', alignItems: 'center', gap: space.xs },
  floorSwatch: { width: 16, height: 1, backgroundColor: colors.muted },
  lowDot: { width: 8, height: 8, borderRadius: 4, backgroundColor: colors.brandBlue },
  legendText: { ...type.small, color: colors.body },
  axis: { flexDirection: 'row', justifyContent: 'space-between', marginTop: space.xs },
  axisText: { ...type.small, color: colors.muted },
  caption: { ...type.small, color: colors.body, marginTop: space.sm },
});

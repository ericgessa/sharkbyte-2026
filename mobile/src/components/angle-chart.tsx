import { useState } from 'react';
import { StyleSheet, Text, View, type LayoutChangeEvent } from 'react-native';
import Svg, { G as Group, Line, Path, Rect, Text as SvgText } from 'react-native-svg';

import { G } from '@/games/theme';
import { formatSeconds, linePath, niceDomain, ticks, type RepBand, type Series } from '@/pose/readings';

export type ChartLine = Series & { color: string };

const PAD = { l: 40, r: 12, t: 12, b: 26 };

/**
 * A line chart of angles (degrees) against time, drawn with react-native-svg so it is the same on web, iOS and Android.
 * Optional shaded bands mark the reps the server counted. Pure drawing: all the numbers arrive already prepared (see
 * pose/readings.ts).
 */
export function AngleChart({
  lines,
  repBands = [],
  durationS,
  height = 190,
  testID,
}: {
  lines: ChartLine[];
  repBands?: RepBand[];
  durationS: number;
  height?: number;
  testID?: string;
}) {
  const [width, setWidth] = useState(640);
  const onLayout = (e: LayoutChangeEvent) => setWidth(Math.max(240, Math.round(e.nativeEvent.layout.width)));

  const plotW = width - PAD.l - PAD.r;
  const plotH = height - PAD.t - PAD.b;
  const x: [number, number] = [0, Math.max(durationS, 1)];
  const y = niceDomain(lines.flatMap((l) => l.points.map((p) => p.v)));
  const sx = (t: number) => PAD.l + (t / x[1]) * plotW;
  const sy = (v: number) => PAD.t + plotH - ((v - y[0]) / (y[1] - y[0])) * plotH;

  return (
    <View onLayout={onLayout} testID={testID}>
      <View style={s.legend}>
        {lines.map((l) => (
          <View key={l.key} style={s.legendItem}>
            <View style={[s.dot, { backgroundColor: l.color }]} />
            <Text style={s.legendText}>{l.label}</Text>
          </View>
        ))}
      </View>
      <Svg width={width} height={height} accessibilityLabel={`Chart: ${lines.map((l) => l.label).join(', ')} over time`}>
        {ticks(y, 4).map((v) => (
          <Group key={`g${v}`}>
            <Line x1={PAD.l} x2={width - PAD.r} y1={sy(v)} y2={sy(v)} stroke={G.line} strokeWidth={1} />
            <SvgText x={PAD.l - 6} y={sy(v) + 4} fontSize={11} fill={G.muted} textAnchor="end">{`${Math.round(v)}°`}</SvgText>
          </Group>
        ))}
        {repBands.map((b, i) => {
          const x0 = sx(Math.max(0, b.t0));
          const w = Math.max(1, sx(Math.min(x[1], b.t1)) - x0);
          return (
            <Group key={`b${b.rep}`}>
              <Rect x={x0} y={PAD.t} width={w} height={plotH} fill={G.green} opacity={i % 2 ? 0.05 : 0.11} />
              {w >= 16 && (
                <SvgText x={x0 + w / 2} y={PAD.t + 11} fontSize={10} fill={G.muted} textAnchor="middle">{`${b.rep}`}</SvgText>
              )}
            </Group>
          );
        })}
        {ticks(x, 5).map((t) => (
          <SvgText key={`x${t}`} x={sx(t)} y={height - 8} fontSize={11} fill={G.muted} textAnchor="middle">
            {formatSeconds(t)}
          </SvgText>
        ))}
        {lines.map((l) => (
          <Path key={l.key} d={linePath(l.points, x, y, plotW, plotH)} transform={`translate(${PAD.l} ${PAD.t})`} stroke={l.color} strokeWidth={2} fill="none" strokeLinejoin="round" strokeLinecap="round" />
        ))}
      </Svg>
    </View>
  );
}

const s = StyleSheet.create({
  legend: { flexDirection: 'row', flexWrap: 'wrap', gap: 14, marginBottom: 6 },
  legendItem: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  dot: { width: 10, height: 10, borderRadius: 5 },
  legendText: { color: G.muted, fontSize: 12, fontWeight: '600' },
});

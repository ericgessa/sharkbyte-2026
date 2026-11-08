/**
 * Rep wave charts, GRITS style (flat colors, rounded bars, no glow):
 *   LiveWave - a scrolling bar wave of the movement while you train (every rep draws a peak)
 *   RepBars  - after the set: one bar per attempt (height = depth, width = time, orange = didn't count,
 *              ink cap = lower-back bend from the spine wearable)
 *   MiniWave - a small RepBars for each recent set in Trends
 */
import { useEffect, useRef, useState } from 'react';
import { StyleSheet, Text, View } from 'react-native';

import { Grits } from '@/constants/grits';
import type { Bar } from '@/exercise/wave';

const LIVE_BARS = 48;
const LIVE_EVERY_MS = 100; // ~5 s of movement on screen

/** `sample()` is read about 10 times a second (it returns the latest frame's value, 0..1, or null). */
export function LiveWave({ sample, height = 64, label }: { sample: () => number | null; height?: number; label?: string }) {
  const [values, setValues] = useState<number[]>(() => Array(LIVE_BARS).fill(0));
  const sampleRef = useRef(sample);
  useEffect(() => {
    sampleRef.current = sample;
  });
  useEffect(() => {
    const id = setInterval(() => {
      const v = sampleRef.current();
      setValues((prev) => [...prev.slice(1), v ?? 0]);
    }, LIVE_EVERY_MS);
    return () => clearInterval(id);
  }, []);

  return (
    <View style={styles.liveWrap} testID="live-wave" accessibilityLabel="Live movement wave">
      <View style={[styles.liveRow, { height }]}>
        {values.map((v, i) => (
          <View
            key={i}
            style={[
              styles.liveBar,
              {
                height: Math.max(3, v * height),
                backgroundColor: i >= LIVE_BARS - 2 ? Grits.orange : v > 0.55 ? Grits.green : Grits.sage,
              },
            ]}
          />
        ))}
      </View>
      {label && <Text style={styles.caption}>{label}</Text>}
    </View>
  );
}

export function RepBars({ bars, height = 120, legend = true, testID }: { bars: Bar[]; height?: number; legend?: boolean; testID?: string }) {
  if (bars.length === 0) return null;
  return (
    <View testID={testID}>
      <View style={[styles.repRow, { height: height + 14 }]}>
        {bars.map((b, i) => (
          <View key={i} style={[styles.repSlot, { flexGrow: b.w, flexBasis: 0 }]}>
            {b.cap !== null && <View style={[styles.cap, { height: 3 + b.cap * 12 }]} />}
            <View
              style={[
                styles.repBar,
                { height: Math.max(6, b.h * height), backgroundColor: b.kind === 'rejected' ? Grits.orange : i % 2 ? Grits.green : Grits.sageDeep },
              ]}
            />
          </View>
        ))}
      </View>
      {legend && (
        <Text style={styles.caption}>
          one bar per rep · taller = deeper · wider = slower{bars.some((b) => b.kind === 'rejected') ? ' · orange = didn’t count' : ''}
          {bars.some((b) => b.cap !== null) ? ' · cap = lower-back bend' : ''}
        </Text>
      )}
    </View>
  );
}

export function MiniWave({ bars, label }: { bars: Bar[]; label: string }) {
  return (
    <View style={styles.mini}>
      {bars.length ? <RepBars bars={bars} height={40} legend={false} /> : <View style={{ height: 54 }} />}
      <Text style={styles.miniLabel} numberOfLines={1}>
        {label}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  liveWrap: { backgroundColor: Grits.cream, borderRadius: 22, paddingVertical: 10, paddingHorizontal: 12, gap: 6, marginTop: 12 },
  liveRow: { flexDirection: 'row', alignItems: 'flex-end', gap: 2 },
  liveBar: { flex: 1, borderTopLeftRadius: 4, borderTopRightRadius: 4 },
  repRow: { flexDirection: 'row', alignItems: 'flex-end', gap: 3 },
  repSlot: { alignItems: 'stretch', justifyContent: 'flex-end', minWidth: 4 },
  repBar: { borderTopLeftRadius: 6, borderTopRightRadius: 6 },
  cap: { backgroundColor: Grits.ink, borderRadius: 2, marginBottom: 2, marginHorizontal: '20%' },
  caption: { color: Grits.inkSoft, fontSize: 12, fontWeight: '700' },
  mini: { width: 92, gap: 4 },
  miniLabel: { color: Grits.inkSoft, fontSize: 11, fontWeight: '700', textAlign: 'center' },
});

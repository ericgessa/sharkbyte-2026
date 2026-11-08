/**
 * Trend facts as a floating, deliberately uneven cluster of bubbles (big word + small caption), a big headline, and
 * the coach's advice in a soft blob. Facts come from the server (computed by code); the headline and advice are worded
 * by the AI coach when it's on. Flat colors from the GRITS palette; React Native Animated only; still under Reduce Motion.
 */
import { useEffect, useState } from 'react';
import { Animated, Easing, Platform, StyleSheet, Text, View } from 'react-native';

import { useReduceMotion } from '@/components/grits-bubbles';
import { MiniWave } from '@/components/rep-wave';
import { Grits } from '@/constants/grits';
import { barsFromServer } from '@/exercise/wave';
import type { TrendFact, Trends } from '@/pose/workout-client';

const NATIVE = Platform.OS !== 'web';

// Size (fraction of the column) by how exciting a fact is, and an uneven vertical offset per position.
const SIZE = { hype: 0.42, warm: 0.34, calm: 0.29 } as const;
const OFFSETS = [0, 28, -8, 20, 2, -12, 24];
const HYPE_COLORS = [Grits.green, Grits.orange];

type Props = { trends: Trends | 'loading' | 'error' | null; column: number; title?: string };

export function TrendBubbles({ trends, column, title }: Props) {
  if (trends === null) return null;
  if (trends === 'error') {
    return (
      <View style={styles.wrap}>
        {title && <Text style={styles.title}>{title}</Text>}
        <Text style={styles.note}>Your trends aren&apos;t available right now.</Text>
      </View>
    );
  }
  if (trends === 'loading') {
    return (
      <View style={styles.wrap} testID="trend-loading">
        {title && <Text style={styles.title}>{title}</Text>}
        <View style={styles.cluster}>
          {[0.3, 0.24, 0.27].map((f, i) => (
            <FloatBubble key={i} index={i} size={column * f} color={Grits.sage} offset={OFFSETS[i]}>
              <Text style={[styles.big, { color: Grits.ink, fontSize: column * f * 0.3 }]}>…</Text>
            </FloatBubble>
          ))}
        </View>
        <Text style={styles.note}>Crunching your sets…</Text>
      </View>
    );
  }

  let hype = 0;
  return (
    <View style={styles.wrap} testID="trend-bubbles">
      {title && <Text style={styles.title}>{title}</Text>}
      <Text style={[styles.headline, { fontSize: Math.min(column * 0.085, 34) }]} testID="trend-headline">
        {trends.headline}
      </Text>
      <View style={styles.cluster}>
        {trends.facts.map((f, i) => {
          const tone = (f.tone in SIZE ? f.tone : 'calm') as keyof typeof SIZE;
          const size = column * SIZE[tone];
          const bg = tone === 'hype' ? HYPE_COLORS[hype++ % 2] : tone === 'warm' ? Grits.sage : Grits.cream;
          const fg = tone === 'hype' ? Grits.pillText : Grits.ink;
          return (
            <FloatBubble key={f.key} index={i} size={size} color={bg} offset={OFFSETS[i % OFFSETS.length]} testID={`trend-fact-${f.key}`}>
              <FactText fact={f} size={size} color={fg} />
            </FloatBubble>
          );
        })}
      </View>
      {trends.waves && trends.waves.length > 1 && (
        <View style={styles.waves} testID="trend-waves">
          <Text style={styles.wavesTitle}>Your last sets, rep by rep</Text>
          <View style={styles.wavesRow}>
            {trends.waves.map((w) => (
              <MiniWave key={w.session_id} bars={barsFromServer(w.bars)} label={`${w.reps} · ${shortDay(w.at)}`} />
            ))}
          </View>
        </View>
      )}
      <View style={styles.advice} testID="trend-advice">
        <View style={styles.adviceTag}>
          <Text style={styles.adviceTagText}>{trends.source === 'agent' ? 'COACH · AI' : 'COACH'}</Text>
        </View>
        <Text style={styles.adviceText}>{trends.advice}</Text>
      </View>
    </View>
  );
}

/** "Mon", "Tue"... or "today". */
function shortDay(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  if (d.toDateString() === new Date().toDateString()) return 'today';
  return ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getDay()];
}

function FactText({ fact, size, color }: { fact: TrendFact; size: number; color: string }) {
  // Long values ("3 days", "12:40") shrink so they stay inside the bubble.
  const big = size * 0.3 * Math.min(1, 3.4 / Math.max(fact.big.length, 1));
  return (
    <>
      <Text style={[styles.big, { color, fontSize: big }]} numberOfLines={1}>
        {fact.big}
      </Text>
      <Text style={[styles.small, { color, fontSize: Math.max(11, size * 0.075), maxWidth: size * 0.74 }]} numberOfLines={3}>
        {fact.small}
      </Text>
    </>
  );
}

function FloatBubble({ index, size, color, offset, children, testID }: { index: number; size: number; color: string; offset: number; children: React.ReactNode; testID?: string }) {
  const reduce = useReduceMotion();
  const [pop] = useState(() => new Animated.Value(0));
  const [bob] = useState(() => new Animated.Value(0));

  useEffect(() => {
    if (reduce === null) return;
    if (reduce) {
      pop.setValue(1);
      return;
    }
    const intro = Animated.sequence([
      Animated.delay(90 * index),
      Animated.spring(pop, { toValue: 1, friction: 5, tension: 75, useNativeDriver: NATIVE }),
    ]);
    const period = 3800 + (index % 3) * 900;
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(bob, { toValue: 1, duration: period / 2, easing: Easing.inOut(Easing.sin), useNativeDriver: NATIVE }),
        Animated.timing(bob, { toValue: 0, duration: period / 2, easing: Easing.inOut(Easing.sin), useNativeDriver: NATIVE }),
      ]),
    );
    intro.start();
    loop.start();
    return () => {
      intro.stop();
      loop.stop();
    };
  }, [reduce, index, pop, bob]);

  const drift = 4 + (index % 3) * 2;
  return (
    <Animated.View
      testID={testID}
      style={[
        styles.bubble,
        {
          width: size,
          height: size,
          borderRadius: size / 2,
          backgroundColor: color,
          marginTop: offset,
          transform: [{ translateY: bob.interpolate({ inputRange: [0, 1], outputRange: [-drift, drift] }) }, { scale: pop }],
        },
      ]}>
      {children}
    </Animated.View>
  );
}

const styles = StyleSheet.create({
  wrap: { marginTop: 18, gap: 10 },
  title: { color: Grits.ink, fontSize: 18, fontWeight: '800' },
  headline: { color: Grits.cream, fontWeight: '900', letterSpacing: -1, lineHeight: 38 },
  note: { color: Grits.inkSoft, fontSize: 14, fontWeight: '600' },
  cluster: { flexDirection: 'row', flexWrap: 'wrap', justifyContent: 'center', alignItems: 'flex-start', columnGap: 4, rowGap: 0, paddingBottom: 26 },
  bubble: { alignItems: 'center', justifyContent: 'center', marginHorizontal: -2 },
  big: { fontWeight: '900', letterSpacing: -1, includeFontPadding: false },
  small: { fontWeight: '700', textAlign: 'center', lineHeight: 15 },
  waves: { backgroundColor: Grits.cream, borderRadius: 28, padding: 16, gap: 10, borderBottomRightRadius: 10 },
  wavesTitle: { color: Grits.ink, fontSize: 15, fontWeight: '800' },
  wavesRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 12, justifyContent: 'space-between' },
  advice: { backgroundColor: Grits.cream, borderRadius: 36, paddingVertical: 20, paddingHorizontal: 22, gap: 8, borderTopLeftRadius: 12 },
  adviceTag: { alignSelf: 'flex-start', backgroundColor: Grits.orange, borderRadius: 999, paddingVertical: 4, paddingHorizontal: 12 },
  adviceTagText: { color: Grits.pillText, fontSize: 12, fontWeight: '900', letterSpacing: 1.2 },
  adviceText: { color: Grits.ink, fontSize: 20, fontWeight: '800', lineHeight: 27 },
});

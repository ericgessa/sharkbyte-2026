import { router, type Href } from 'expo-router';
import { useEffect, useState } from 'react';
import { AccessibilityInfo, Animated, Easing, Platform, Pressable, StyleSheet, Text, useWindowDimensions, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { Grits } from '@/constants/grits';

/**
 * The GRITS home screen: the wordmark on a field of weight plates, and two ways in.
 * Layout follows the mockup's proportions (measured on a 340 x 740 frame), scaled to the screen.
 * Pure React Native `Animated`: no extra libraries, nothing that talks to the backend.
 */

// Plates as fractions of the frame: centre x, centre y, radius (of the frame width).
const PLATES = [
  { x: 0.91, y: 0.09, r: 0.23, drift: 5, period: 7000 },
  { x: 0.4, y: 0.29, r: 0.23, drift: 6, period: 8200 },
  { x: 0.77, y: 0.555, r: 0.235, drift: 4, period: 6400 },
  { x: 0.35, y: 0.88, r: 0.235, drift: 7, period: 9000 },
];

const NATIVE = Platform.OS !== 'web'; // the native animation driver doesn't exist on web
const FRAME_MAX_WIDTH = 520; // on a wide browser the composition stays phone-shaped, centred

export function GritsHome() {
  const { width, height } = useWindowDimensions();
  const frameW = Math.min(width, FRAME_MAX_WIDTH);
  const reduceMotion = useReduceMotion();

  // Intro: bar loads, wordmark settles, then the two pills arrive one after the other.
  const [bar] = useState(() => new Animated.Value(0));
  const [word] = useState(() => new Animated.Value(0));
  const [pills] = useState(() => [new Animated.Value(0), new Animated.Value(0)]);

  useEffect(() => {
    if (reduceMotion === null) return; // not known yet
    const all = [bar, word, ...pills];
    if (reduceMotion) {
      all.forEach((v) => v.setValue(1));
      return;
    }
    const ease = Easing.out(Easing.cubic);
    const run = Animated.sequence([
      Animated.timing(word, { toValue: 1, duration: 420, easing: ease, useNativeDriver: NATIVE }),
      Animated.timing(bar, { toValue: 1, duration: 520, easing: Easing.inOut(Easing.cubic), useNativeDriver: false }),
      Animated.stagger(110, pills.map((p) => Animated.timing(p, { toValue: 1, duration: 360, easing: ease, useNativeDriver: NATIVE }))),
    ]);
    run.start();
    return () => run.stop();
  }, [reduceMotion, bar, word, pills]);

  const barWidth = frameW * 0.72;

  return (
    <View style={styles.screen}>
      <View style={[styles.frame, { width: frameW }]}>
        {PLATES.map((p, i) => (
          <Plate key={i} cx={p.x * frameW} cy={p.y * height} r={p.r * frameW} drift={p.drift} period={p.period} still={reduceMotion !== false} />
        ))}

        <SafeAreaView style={styles.content} edges={['top', 'bottom']}>
          <View style={[styles.block, { marginTop: height * 0.36 }]}>
            <Animated.Text
              accessibilityRole="header"
              style={[
                styles.wordmark,
                { fontSize: Math.min(frameW * 0.25, 120), opacity: word, transform: [{ translateY: word.interpolate({ inputRange: [0, 1], outputRange: [14, 0] }) }] },
              ]}>
              GRITS
            </Animated.Text>
            <View style={[styles.barTrack, { width: barWidth }]}>
              <Animated.View style={[styles.bar, { width: bar.interpolate({ inputRange: [0, 1], outputRange: [0, barWidth] }) }]} />
            </View>

            <View style={styles.pills}>
              <Pill label="Exercise" hint="Track your reps with the camera and the spine wearable" progress={pills[0]} onPress={() => router.push('/exercise' as Href)} />
              <Pill label="Play" hint="GRIT Games" progress={pills[1]} onPress={() => router.push('/games')} />
            </View>
          </View>
        </SafeAreaView>
      </View>
    </View>
  );
}

/** A weight plate: a sage disc with a peach centre hole, bobbing a few pixels like breathing between reps. */
function Plate({ cx, cy, r, drift, period, still }: { cx: number; cy: number; r: number; drift: number; period: number; still: boolean }) {
  const [t] = useState(() => new Animated.Value(0));
  useEffect(() => {
    if (still) return;
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(t, { toValue: 1, duration: period / 2, easing: Easing.inOut(Easing.sin), useNativeDriver: NATIVE }),
        Animated.timing(t, { toValue: 0, duration: period / 2, easing: Easing.inOut(Easing.sin), useNativeDriver: NATIVE }),
      ]),
    );
    loop.start();
    return () => loop.stop();
  }, [still, period, t]);

  const hole = Math.max(6, r * 0.13);
  return (
    <Animated.View
      pointerEvents="none"
      importantForAccessibility="no-hide-descendants"
      accessibilityElementsHidden
      style={[
        styles.plate,
        {
          left: cx - r,
          top: cy - r,
          width: r * 2,
          height: r * 2,
          borderRadius: r,
          transform: [{ translateY: t.interpolate({ inputRange: [0, 1], outputRange: [-drift, drift] }) }],
        },
      ]}>
      <View style={[styles.hole, { width: hole * 2, height: hole * 2, borderRadius: hole }]} />
    </Animated.View>
  );
}

function Pill({ label, hint, progress, onPress }: { label: string; hint: string; progress: Animated.Value; onPress: () => void }) {
  return (
    <Animated.View
      style={{ opacity: progress, transform: [{ translateY: progress.interpolate({ inputRange: [0, 1], outputRange: [14, 0] }) }] }}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={label}
        accessibilityHint={hint}
        testID={`home-${label.toLowerCase()}`}
        onPress={onPress}
        style={({ pressed }) => [styles.pill, pressed && styles.pillPressed]}>
        <Text style={styles.pillText}>{label}</Text>
      </Pressable>
    </Animated.View>
  );
}

/** null until known, then whether the user asked the OS for less motion. */
function useReduceMotion(): boolean | null {
  const [reduce, setReduce] = useState<boolean | null>(null);
  useEffect(() => {
    let alive = true;
    AccessibilityInfo.isReduceMotionEnabled()
      .then((v) => alive && setReduce(v))
      .catch(() => alive && setReduce(false));
    const sub = AccessibilityInfo.addEventListener('reduceMotionChanged', setReduce);
    return () => {
      alive = false;
      sub.remove();
    };
  }, []);
  return reduce;
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: Grits.peach, alignItems: 'center', overflow: 'hidden' },
  frame: { flex: 1, position: 'relative' },
  plate: { position: 'absolute', backgroundColor: Grits.sage, alignItems: 'center', justifyContent: 'center' },
  hole: { backgroundColor: Grits.peach },
  content: { flex: 1 },
  block: { paddingHorizontal: '13%' },
  wordmark: { color: Grits.cream, fontWeight: '900', letterSpacing: -2, includeFontPadding: false },
  barTrack: { height: 14, marginTop: 6 },
  bar: { height: 14, backgroundColor: Grits.green },
  pills: { marginTop: 34, gap: 18, alignItems: 'center' },
  pill: { backgroundColor: Grits.pill, borderRadius: 999, paddingVertical: 18, paddingHorizontal: 40, minWidth: 230, alignItems: 'center' },
  pillPressed: { backgroundColor: Grits.pillPressed, transform: [{ translateY: 2 }] },
  pillText: { color: Grits.pillText, fontSize: 24, fontWeight: '800', letterSpacing: 0.3 },
});

/**
 * The animated bubble field behind the exercise screens. Three depths:
 *   back  - big, slow, sage, partly see-through
 *   mid   - medium sage and green
 *   front - small and quick, green / deep sage / orange accents
 * Each bubble pops in (staggered), then drifts on its own slow loop; nearer bubbles move further and faster,
 * which reads as depth. Flat colors only (no glow, blur or shadow). React Native `Animated`, nothing else.
 * Everything holds still when the OS asks for reduced motion.
 */
import { useEffect, useState } from 'react';
import { AccessibilityInfo, Animated, Easing, Platform, StyleSheet, useWindowDimensions, View } from 'react-native';

import { Grits } from '@/constants/grits';

const NATIVE = Platform.OS !== 'web';

type Bubble = {
  x: number; // centre, fraction of the column width
  y: number; // centre, fraction of the screen height
  r: number; // radius, fraction of the column width
  color: string;
  opacity: number;
  drift: number; // px of travel each way
  period: number; // ms for one full drift loop
};

// Solid colors only: see-through sage over peach mixes into an off-palette khaki. Depth comes from size and speed.
const back = (x: number, y: number, r: number): Bubble => ({ x, y, r, color: Grits.sage, opacity: 1, drift: 5, period: 16000 });
const mid = (x: number, y: number, r: number, color: string = Grits.sage): Bubble => ({ x, y, r, color, opacity: 1, drift: 10, period: 10000 });
const front = (x: number, y: number, r: number, color: string): Bubble => ({ x, y, r, color, opacity: 1, drift: 16, period: 6500 });

/**
 * A few compositions, so each screen has its own field. Bubbles keep to the edges: the middle column, where the
 * title, camera and buttons sit, stays clear (and no green/orange bubble sits behind a green/orange button).
 */
export const BUBBLE_SETS: Record<'intro' | 'set' | 'hub' | 'results', Bubble[]> = {
  intro: [
    back(0.02, 0.04, 0.3), back(1.02, 0.08, 0.28), back(0.55, 1.02, 0.34),
    mid(0.0, 0.36, 0.14), mid(1.0, 0.62, 0.2),
    front(0.93, 0.33, 0.045, Grits.orange), front(0.07, 0.68, 0.04, Grits.sageDeep), front(0.9, 0.86, 0.035, Grits.green),
  ],
  set: [
    back(0.98, 0.02, 0.24), back(0.0, 0.62, 0.26), back(1.0, 0.9, 0.28),
    mid(0.2, 1.0, 0.2),
    front(0.03, 0.22, 0.035, Grits.orange), front(0.72, 0.97, 0.04, Grits.sageDeep), front(0.99, 0.5, 0.04, Grits.green),
  ],
  hub: [
    back(1.0, 0.1, 0.28), back(0.0, 0.62, 0.24),
    mid(0.96, 0.78, 0.12),
    front(0.06, 0.24, 0.035, Grits.orange), front(0.62, 0.99, 0.045, Grits.sageDeep),
  ],
  results: [
    back(0.0, 0.06, 0.26), back(1.0, 0.52, 0.26),
    mid(0.12, 0.96, 0.16), front(0.92, 0.14, 0.045, Grits.orange), front(0.04, 0.46, 0.035, Grits.green),
  ],
};

const ACCENTS: string[] = [Grits.orange, Grits.green, Grits.sageDeep];

/** The same composition, varied: odd variants are mirrored, accent colors rotate, sizes wobble a little per bubble. */
export function varyBubbles(list: Bubble[], variant: number): Bubble[] {
  if (!variant) return list;
  return list.map((b, i) => {
    const accent = ACCENTS.indexOf(b.color);
    return {
      ...b,
      x: variant % 2 === 1 ? 1 - b.x : b.x,
      r: b.r * (1 + 0.14 * Math.sin(variant * 7.3 + i * 2.1)),
      color: accent >= 0 ? ACCENTS[(accent + variant) % ACCENTS.length] : b.color, // big bubbles stay sage: never the color of a fact bubble
    };
  });
}

export function Bubbles({ set, columnWidth, variant = 0 }: { set: keyof typeof BUBBLE_SETS; columnWidth: number; variant?: number }) {
  const { height } = useWindowDimensions();
  const reduce = useReduceMotion();
  return (
    <View pointerEvents="none" style={styles.layer} importantForAccessibility="no-hide-descendants" accessibilityElementsHidden>
      <View style={{ width: columnWidth, height: '100%' }}>
        {varyBubbles(BUBBLE_SETS[set], variant).map((b, i) => (
          <BubbleView key={i} b={b} index={i} column={columnWidth} height={height} motion={reduce} />
        ))}
      </View>
    </View>
  );
}

/** motion: null = not known yet (stay hidden a moment), true = reduce motion (just show), false = animate. */
function BubbleView({ b, index, column, height, motion }: { b: Bubble; index: number; column: number; height: number; motion: boolean | null }) {
  const [pop] = useState(() => new Animated.Value(0));
  const [t] = useState(() => new Animated.Value(0));

  useEffect(() => {
    if (motion === null) return;
    if (motion) {
      pop.setValue(1);
      return;
    }
    const intro = Animated.sequence([
      Animated.delay(60 * index),
      Animated.spring(pop, { toValue: 1, friction: 5, tension: 70, useNativeDriver: NATIVE }),
    ]);
    const drift = Animated.loop(
      Animated.sequence([
        Animated.timing(t, { toValue: 1, duration: b.period / 2, easing: Easing.inOut(Easing.sin), useNativeDriver: NATIVE }),
        Animated.timing(t, { toValue: 0, duration: b.period / 2, easing: Easing.inOut(Easing.sin), useNativeDriver: NATIVE }),
      ]),
    );
    intro.start();
    drift.start();
    return () => {
      intro.stop();
      drift.stop();
    };
  }, [motion, index, b.period, pop, t]);

  const r = b.r * column;
  // Odd bubbles drift the other way, so the field never moves as one block.
  const dir = index % 2 === 0 ? 1 : -1;
  return (
    <Animated.View
      style={{
        position: 'absolute',
        left: b.x * column - r,
        top: b.y * height - r,
        width: r * 2,
        height: r * 2,
        borderRadius: r,
        backgroundColor: b.color,
        opacity: b.opacity,
        transform: [
          { translateX: t.interpolate({ inputRange: [0, 1], outputRange: [-b.drift * dir, b.drift * dir] }) },
          { translateY: t.interpolate({ inputRange: [0, 1], outputRange: [b.drift * 0.7, -b.drift * 0.7] }) },
          { scale: pop },
        ],
      }}
    />
  );
}

/** null until known, then whether the user asked the OS for less motion. */
export function useReduceMotion(): boolean | null {
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
  layer: { ...StyleSheet.absoluteFill, alignItems: 'center', overflow: 'hidden' },
});

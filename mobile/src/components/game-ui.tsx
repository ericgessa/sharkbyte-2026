/**
 * The Games hub and Challenges in the GRIT look, the same as Home and Exercise: peach page with the drifting bubbles,
 * the cream wordmark with its green bar, cream cards with a small sage weight plate, pill buttons. Flat colours only.
 * Cards pop in one after another (still under Reduce Motion).
 */
import { router, type Href } from 'expo-router';
import { useEffect, useState, type ReactNode } from 'react';
import { Animated, Platform, Pressable, StyleSheet, Text, View, type StyleProp, type TextStyle, type ViewStyle } from 'react-native';

import { useReduceMotion } from '@/components/grits-bubbles';
import { GRITS_MAX_WIDTH, GritsButton, GritsHeader, GritsScreen, Plate } from '@/components/grits-ui';
import { Grits } from '@/constants/grits';

const NATIVE = Platform.OS !== 'web';
export const GAME_MAX_WIDTH = GRITS_MAX_WIDTH;
const CARD_PRESSED = '#F7D7B5'; // the cream card, a shade deeper while pressed

export function GameScreen({ title, back = 'Home', backTo = '/', children }: { title: string; back?: string; backTo?: Href; children: ReactNode }) {
  return (
    <GritsScreen bubbles="hub" variant={1}>
      <GameTitle text={title} back={back} backTo={backTo} />
      <View style={styles.stack}>{children}</View>
    </GritsScreen>
  );
}

/** The GRIT wordmark header: "‹ Back", the cream title, the green bar. */
export function GameTitle({ text, back, backTo = '/' }: { text: string; back?: string; backTo?: Href }) {
  return <GritsHeader title={text} back={back} onBack={() => (router.canGoBack() ? router.back() : router.replace(backTo))} />;
}

/** A cream card with a small sage weight plate in the corner; pops in, presses in. */
export function GameCard({ index, title, kicker, children, onPress, dim, testID, style }: { index: number; title: string; kicker?: string; children?: ReactNode; onPress?: () => void; dim?: boolean; testID?: string; style?: StyleProp<ViewStyle> }) {
  const reduce = useReduceMotion();
  const [pop] = useState(() => new Animated.Value(0));
  useEffect(() => {
    if (reduce === null) return;
    if (reduce) {
      pop.setValue(1);
      return;
    }
    const a = Animated.sequence([Animated.delay(80 * index), Animated.spring(pop, { toValue: 1, friction: 6, tension: 70, useNativeDriver: NATIVE })]);
    a.start();
    return () => a.stop();
  }, [reduce, index, pop]);

  const body = (
    <>
      <View style={styles.cardHead}>
        <View style={styles.cardHeadText}>
          {kicker && <Text style={styles.kicker}>{kicker}</Text>}
          <Text style={[styles.cardTitle, dim && { color: Grits.inkSoft }]}>{title}</Text>
        </View>
        <Plate size={44} hole style={dim ? styles.plateDim : undefined} />
      </View>
      {children}
    </>
  );
  return (
    <Animated.View style={{ opacity: pop, transform: [{ translateY: pop.interpolate({ inputRange: [0, 1], outputRange: [24, 0] }) }] }}>
      {onPress ? (
        <Pressable accessibilityRole="button" accessibilityLabel={title} onPress={onPress} testID={testID} style={({ pressed }) => [styles.card, style, pressed && styles.cardPressed]}>
          {body}
        </Pressable>
      ) : (
        <View style={[styles.card, style]} testID={testID}>
          {body}
        </View>
      )}
    </Animated.View>
  );
}

export function GameText({ children, muted, size = 16, weight = '700', style }: { children: ReactNode; muted?: boolean; size?: number; weight?: '600' | '700' | '800' | '900'; style?: StyleProp<TextStyle> }) {
  return <Text style={[{ color: muted ? Grits.inkSoft : Grits.ink, fontSize: size, fontWeight: weight, lineHeight: Math.round(size * 1.35) }, style]}>{children}</Text>;
}

/** 'coral' (the main action) is the GRIT pill; 'ghost' is the outlined pill. */
export function GameButton({ label, onPress, tone = 'coral', testID }: { label: string; onPress: () => void; tone?: 'coral' | 'ghost'; testID?: string }) {
  return (
    <View style={styles.buttonWrap}>
      <GritsButton label={label} onPress={onPress} tone={tone === 'ghost' ? 'ghost' : 'pill'} testID={testID} />
    </View>
  );
}

const styles = StyleSheet.create({
  stack: { gap: 16 },
  card: { backgroundColor: Grits.cream, borderRadius: 26, paddingVertical: 20, paddingHorizontal: 20, gap: 8 },
  cardPressed: { backgroundColor: CARD_PRESSED, transform: [{ scale: 0.985 }] },
  cardHead: { flexDirection: 'row', alignItems: 'center', gap: 12 },
  cardHeadText: { flex: 1, gap: 2 },
  kicker: { color: Grits.sageDeep, fontSize: 12, fontWeight: '900', letterSpacing: 1.6 },
  cardTitle: { color: Grits.ink, fontSize: 24, fontWeight: '900', letterSpacing: -0.4 },
  plateDim: { opacity: 0.45 },
  buttonWrap: { alignSelf: 'flex-start' },
});

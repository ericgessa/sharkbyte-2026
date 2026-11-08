/**
 * Small building blocks shared by the GRIT screens (home, exercise hub, session). Flat colors from
 * constants/grits.ts: no glow, shadows or gradients. Only React Native core.
 */
import { router } from 'expo-router';
import type { ReactNode } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, useWindowDimensions, View, type StyleProp, type ViewStyle } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { BUBBLE_SETS, Bubbles } from '@/components/grits-bubbles';
import { Grits } from '@/constants/grits';

export const GRITS_MAX_WIDTH = 560;

/** Peach page, centred phone-width column, scrolls when it has to. `bubbles` adds the animated field behind it. */
export function GritsScreen({ children, scroll = true, bubbles, variant = 0 }: { children: ReactNode; scroll?: boolean; bubbles?: keyof typeof BUBBLE_SETS; variant?: number }) {
  const { width } = useWindowDimensions();
  const body = <View style={styles.column}>{children}</View>;
  return (
    <View style={styles.page}>
      {bubbles && <Bubbles set={bubbles} columnWidth={Math.min(width, GRITS_MAX_WIDTH)} variant={variant} />}
      <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
        {scroll ? (
          <ScrollView contentContainerStyle={styles.scroll} keyboardShouldPersistTaps="handled">
            {body}
          </ScrollView>
        ) : (
          body
        )}
      </SafeAreaView>
    </View>
  );
}

/** "‹ Back" in ink, then a cream wordmark title with the green bar under it (the home screen's mark, smaller). */
export function GritsHeader({ title, back, onBack }: { title: string; back?: string; onBack?: () => void }) {
  return (
    <View style={styles.header}>
      {back && (
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={`Back to ${back}`}
          hitSlop={12}
          onPress={onBack ?? (() => (router.canGoBack() ? router.back() : router.replace('/')))}>
          <Text style={styles.back}>‹ {back}</Text>
        </Pressable>
      )}
      <Text accessibilityRole="header" style={styles.title}>
        {title}
      </Text>
      <View style={styles.bar} />
    </View>
  );
}

export function GritsButton({
  label,
  onPress,
  disabled,
  tone = 'pill',
  big,
  testID,
}: {
  label: string;
  onPress: () => void;
  disabled?: boolean;
  tone?: 'pill' | 'sage' | 'green' | 'orange' | 'ghost';
  big?: boolean;
  testID?: string;
}) {
  const bg = { pill: Grits.pill, sage: Grits.sageDeep, green: Grits.green, orange: Grits.orange, ghost: 'transparent' }[tone];
  const pressedBg = { pill: Grits.pillPressed, sage: Grits.sageDeep, green: Grits.greenPressed, orange: Grits.orangePressed, ghost: 'transparent' }[tone];
  const fg = tone === 'ghost' ? Grits.ink : Grits.pillText;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ disabled: !!disabled }}
      disabled={disabled}
      onPress={onPress}
      testID={testID}
      style={({ pressed }) => [
        styles.button,
        big && styles.buttonBig,
        { backgroundColor: bg },
        tone === 'ghost' && styles.ghost,
        pressed && !disabled && styles.pressed,
        pressed && !disabled && { backgroundColor: pressedBg },
        disabled && styles.disabled,
      ]}>
      <Text style={[styles.buttonText, big && styles.buttonTextBig, { color: fg }]}>{label}</Text>
    </Pressable>
  );
}

/** A sage weight plate (disc with a peach centre hole optional) holding content. */
export function Plate({ size, children, hole = false, style }: { size: number; children?: ReactNode; hole?: boolean; style?: StyleProp<ViewStyle> }) {
  return (
    <View style={[styles.plate, { width: size, height: size, borderRadius: size / 2 }, style]}>
      {hole && <View style={[styles.hole, { width: size * 0.09, height: size * 0.09, borderRadius: size }]} />}
      {children}
    </View>
  );
}

export function Ink({ children, size = 16, weight = '600', soft, center, style }: { children: ReactNode; size?: number; weight?: '400' | '600' | '700' | '800' | '900'; soft?: boolean; center?: boolean; style?: StyleProp<import('react-native').TextStyle> }) {
  return <Text style={[{ color: soft ? Grits.inkSoft : Grits.ink, fontSize: size, fontWeight: weight, lineHeight: Math.round(size * 1.35) }, center && { textAlign: 'center' }, style]}>{children}</Text>;
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: Grits.peach, overflow: 'hidden' },
  safe: { flex: 1 },
  scroll: { flexGrow: 1, alignItems: 'center' },
  column: { width: '100%', maxWidth: GRITS_MAX_WIDTH, paddingHorizontal: 22, paddingBottom: 32, flexGrow: 1 },
  header: { paddingTop: 10, paddingBottom: 18, gap: 4 },
  back: { color: Grits.ink, fontSize: 16, fontWeight: '700', marginBottom: 6 },
  title: { color: Grits.cream, fontSize: 52, fontWeight: '900', letterSpacing: -1.5, lineHeight: 56 },
  bar: { height: 10, width: '62%', backgroundColor: Grits.green },
  button: { borderRadius: 999, paddingVertical: 13, paddingHorizontal: 26, alignItems: 'center', justifyContent: 'center', minWidth: 140 },
  buttonBig: { paddingVertical: 17, paddingHorizontal: 34, minWidth: 220 },
  ghost: { borderWidth: 2, borderColor: Grits.pill },
  pressed: { transform: [{ translateY: 2 }] },
  disabled: { opacity: 0.45 },
  buttonText: { fontSize: 18, fontWeight: '800', letterSpacing: 0.2 },
  buttonTextBig: { fontSize: 22 },
  plate: { backgroundColor: Grits.sage, alignItems: 'center', justifyContent: 'center', overflow: 'hidden' },
  hole: { position: 'absolute', backgroundColor: Grits.peach },
});

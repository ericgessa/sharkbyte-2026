/**
 * The Games screens' building blocks in the GRIT look (Home / Exercise): peach page, cream wordmark with the green
 * bar, cream cards, pill buttons, sage accents. Flat colours, no shadows or gradients.
 *
 * Same components and props as ./ui (which History and the workout detail still use, in charcoal), so a Games
 * screen switches look by changing its import, and nothing about how it works.
 */
import { router } from 'expo-router';
import type { ReactNode } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, View, type StyleProp, type TextStyle, type ViewStyle } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { GritsHeader } from '@/components/grits-ui';
import { Grits } from '@/constants/grits';
import { GG as G, MAX_W } from '@/games/theme';

/** Peach page with the GRIT wordmark header ("‹ Games", the cream title and its green bar). Wide enough for the cameras. */
export function GameScreen({ title, subtitle, back = '/games', children }: { title: string; subtitle?: string; back?: '/games' | '/'; children: ReactNode }) {
  return (
    <View style={s.page}>
      <SafeAreaView style={{ flex: 1 }} edges={['top', 'bottom']}>
        <ScrollView contentContainerStyle={s.pageInner} keyboardShouldPersistTaps="handled">
          <View style={s.column}>
            <View testID="games-back">
              <GritsHeader
                title={title}
                back={back === '/' ? 'Home' : 'Games'}
                onBack={() => (router.canGoBack() ? router.back() : router.replace(back))}
              />
            </View>
            {subtitle ? <Text style={s.sub}>{subtitle}</Text> : null}
            {children}
          </View>
        </ScrollView>
      </SafeAreaView>
    </View>
  );
}

export function Panel({ children, style, testID }: { children: ReactNode; style?: StyleProp<ViewStyle>; testID?: string }) {
  return (
    <View style={[s.panel, style]} testID={testID}>
      {children}
    </View>
  );
}

export function SectionTitle({ children }: { children: ReactNode }) {
  return <Text style={s.sectionTitle}>{children}</Text>;
}

export function Body({ children, style, testID }: { children: ReactNode; style?: StyleProp<TextStyle>; testID?: string }) {
  return (
    <Text style={[s.body, style]} testID={testID}>
      {children}
    </Text>
  );
}

export function Btn({
  label,
  onPress,
  variant = 'primary',
  disabled,
  testID,
  style,
}: {
  label: string;
  onPress: () => void;
  variant?: 'primary' | 'ghost' | 'danger';
  disabled?: boolean;
  testID?: string;
  style?: StyleProp<ViewStyle>;
}) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ disabled: !!disabled }}
      disabled={disabled}
      onPress={onPress}
      testID={testID}
      style={({ pressed }) => [
        s.btn,
        variant === 'primary' && s.btnPrimary,
        variant === 'ghost' && s.btnGhost,
        variant === 'danger' && s.btnDanger,
        disabled && s.disabled,
        pressed && !disabled && s.pressed,
        style,
      ]}>
      <Text style={[s.btnText, variant === 'primary' && s.btnTextPrimary, variant === 'danger' && { color: G.red }]}>{label}</Text>
    </Pressable>
  );
}

export function Chip({ label, selected, onPress, disabled, testID }: { label: string; selected: boolean; onPress: () => void; disabled?: boolean; testID?: string }) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ selected, disabled: !!disabled }}
      disabled={disabled}
      onPress={onPress}
      testID={testID}
      style={({ pressed }) => [s.chip, selected && s.chipOn, disabled && s.disabled, pressed && !disabled && s.pressed]}>
      <Text style={[s.chipText, selected && s.chipTextOn]}>{label}</Text>
    </Pressable>
  );
}

export function Stat({ label, value, tone, testID }: { label: string; value: string | number; tone?: 'green' | 'amber' | 'red'; testID?: string }) {
  return (
    <View style={s.stat}>
      <Text style={s.statLabel}>{label}</Text>
      <Text style={[s.statValue, tone === 'green' && { color: G.green }, tone === 'amber' && { color: G.amber }, tone === 'red' && { color: G.red }]} testID={testID}>
        {value}
      </Text>
    </View>
  );
}

export function Row({ children, style }: { children: ReactNode; style?: StyleProp<ViewStyle> }) {
  return <View style={[s.row, style]}>{children}</View>;
}

export function Steps({ items }: { items: string[] }) {
  return (
    <View style={{ gap: 8 }}>
      {items.map((t, i) => (
        <View key={t} style={s.step}>
          <Text style={s.stepNum}>{i + 1}</Text>
          <Text style={[s.body, { flex: 1 }]}>{t}</Text>
        </View>
      ))}
    </View>
  );
}

/** A small coloured label, e.g. the camera status or "PRACTICE: not saved". */
export function Badge({ label, tone = 'muted', testID }: { label: string; tone?: 'green' | 'amber' | 'red' | 'muted' | 'blue'; testID?: string }) {
  const color = { green: G.green, amber: G.amber, red: G.red, muted: G.muted, blue: G.blue }[tone];
  return (
    <View style={[s.badge, { borderColor: color }]} testID={testID}>
      <View style={[s.dot, { backgroundColor: color }]} />
      <Text style={[s.badgeText, { color }]}>{label}</Text>
    </View>
  );
}

/** Text inputs on the Games screens (codes, names). */
export const inputStyle: TextStyle = {
  backgroundColor: G.panelHi,
  color: G.text,
  borderColor: G.line,
  borderWidth: 2,
  borderRadius: 14,
  paddingVertical: 9,
  paddingHorizontal: 12,
  fontSize: 15,
  fontWeight: '600',
};

const s = StyleSheet.create({
  page: { flex: 1, backgroundColor: G.bg },
  pageInner: { alignItems: 'center', paddingHorizontal: 22, paddingBottom: 32, flexGrow: 1 },
  column: { width: '100%', maxWidth: MAX_W, gap: 14 },
  sub: { color: G.muted, fontSize: 17, fontWeight: '600', marginTop: -6 },
  panel: { backgroundColor: G.panel, borderRadius: 22, padding: 18, gap: 10 },
  sectionTitle: { color: G.green, fontSize: 12, fontWeight: '900', letterSpacing: 1.6, textTransform: 'uppercase' },
  body: { color: G.text, fontSize: 15, lineHeight: 22, fontWeight: '600' },
  btn: { paddingVertical: 12, paddingHorizontal: 24, borderRadius: 999, alignItems: 'center', justifyContent: 'center', backgroundColor: Grits.pill },
  btnPrimary: { backgroundColor: G.green },
  btnGhost: { backgroundColor: 'transparent', borderWidth: 2, borderColor: Grits.pill },
  btnDanger: { backgroundColor: 'transparent', borderWidth: 2, borderColor: G.red },
  btnText: { color: Grits.pillText, fontSize: 16, fontWeight: '800' },
  btnTextPrimary: { color: G.greenInk },
  chip: { paddingVertical: 8, paddingHorizontal: 16, borderRadius: 999, borderWidth: 2, borderColor: G.line, backgroundColor: G.panelHi },
  chipOn: { borderColor: G.green, backgroundColor: G.greenDim },
  chipText: { color: G.muted, fontSize: 14, fontWeight: '800' },
  chipTextOn: { color: G.text },
  disabled: { opacity: 0.45 },
  pressed: { transform: [{ translateY: 2 }] },
  stat: { minWidth: 92, gap: 2 },
  statLabel: { color: G.muted, fontSize: 11, fontWeight: '800', letterSpacing: 1.5, textTransform: 'uppercase' },
  statValue: { color: G.text, fontSize: 28, fontWeight: '900', fontVariant: ['tabular-nums'] },
  row: { flexDirection: 'row', flexWrap: 'wrap', gap: 10, alignItems: 'center' },
  step: { flexDirection: 'row', gap: 10, alignItems: 'flex-start' },
  stepNum: { color: G.greenInk, backgroundColor: G.green, width: 24, height: 24, borderRadius: 12, textAlign: 'center', fontWeight: '900', fontSize: 13, lineHeight: 24, overflow: 'hidden' },
  badge: { flexDirection: 'row', alignItems: 'center', gap: 6, borderWidth: 2, borderRadius: 999, paddingVertical: 4, paddingHorizontal: 10, backgroundColor: G.panelHi },
  dot: { width: 8, height: 8, borderRadius: 4 },
  badgeText: { fontSize: 12, fontWeight: '900', letterSpacing: 0.5 },
});

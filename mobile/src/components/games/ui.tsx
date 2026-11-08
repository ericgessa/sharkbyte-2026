import { router } from 'expo-router';
import type { ReactNode } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, View, type StyleProp, type TextStyle, type ViewStyle } from 'react-native';

import { G, MAX_W } from '@/games/theme';

/** Full-screen charcoal page with a back button, centred content and a max width (desktop first, fine on a phone). */
export function GameScreen({ title, subtitle, back = '/games', children }: { title: string; subtitle?: string; back?: '/games' | '/'; children: ReactNode }) {
  return (
    <ScrollView style={s.page} contentContainerStyle={s.pageInner}>
      <View style={s.column}>
        <View style={s.topRow}>
          <Pressable accessibilityRole="button" onPress={() => (router.canGoBack() ? router.back() : router.replace(back))} style={s.back} testID="games-back">
            <Text style={s.backText}>← Back</Text>
          </Pressable>
          <Text style={s.brand}>GRIT GAMES</Text>
        </View>
        <Text style={s.h1} accessibilityRole="header">
          {title}
        </Text>
        {subtitle ? <Text style={s.sub}>{subtitle}</Text> : null}
        {children}
      </View>
    </ScrollView>
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
      style={({ pressed }) => [s.btn, variant === 'primary' && s.btnPrimary, variant === 'ghost' && s.btnGhost, variant === 'danger' && s.btnDanger, disabled && s.disabled, pressed && s.pressed, style]}>
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
      style={({ pressed }) => [s.chip, selected && s.chipOn, disabled && s.disabled, pressed && s.pressed]}>
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
    <View style={{ gap: 6 }}>
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

const s = StyleSheet.create({
  page: { flex: 1, backgroundColor: G.bg },
  pageInner: { alignItems: 'center', paddingVertical: 20, paddingHorizontal: 16 },
  column: { width: '100%', maxWidth: MAX_W, gap: 14 },
  topRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  back: { paddingVertical: 6, paddingRight: 12 },
  backText: { color: G.muted, fontSize: 15, fontWeight: '600' },
  brand: { color: G.green, fontSize: 12, fontWeight: '800', letterSpacing: 3 },
  h1: { color: G.text, fontSize: 40, fontWeight: '800', letterSpacing: -0.5 },
  sub: { color: G.muted, fontSize: 17, marginTop: -6 },
  panel: { backgroundColor: G.panel, borderColor: G.line, borderWidth: 1, borderRadius: 16, padding: 16, gap: 10 },
  sectionTitle: { color: G.green, fontSize: 12, fontWeight: '800', letterSpacing: 2, textTransform: 'uppercase' },
  body: { color: G.text, fontSize: 15, lineHeight: 22 },
  btn: { paddingVertical: 12, paddingHorizontal: 20, borderRadius: 12, alignItems: 'center', justifyContent: 'center', borderWidth: 1, borderColor: G.line, backgroundColor: G.panelHi },
  btnPrimary: { backgroundColor: G.green, borderColor: G.green },
  btnGhost: { backgroundColor: 'transparent' },
  btnDanger: { backgroundColor: 'transparent', borderColor: G.red },
  btnText: { color: G.text, fontSize: 15, fontWeight: '700' },
  btnTextPrimary: { color: G.greenInk, fontWeight: '800' },
  chip: { paddingVertical: 8, paddingHorizontal: 14, borderRadius: 999, borderWidth: 1, borderColor: G.line, backgroundColor: G.panelHi },
  chipOn: { borderColor: G.green, backgroundColor: '#12310f' },
  chipText: { color: G.muted, fontSize: 14, fontWeight: '700' },
  chipTextOn: { color: G.green },
  disabled: { opacity: 0.4 },
  pressed: { opacity: 0.75 },
  stat: { minWidth: 92, gap: 2 },
  statLabel: { color: G.muted, fontSize: 11, fontWeight: '700', letterSpacing: 1.5, textTransform: 'uppercase' },
  statValue: { color: G.text, fontSize: 28, fontWeight: '800', fontVariant: ['tabular-nums'] },
  row: { flexDirection: 'row', flexWrap: 'wrap', gap: 10, alignItems: 'center' },
  step: { flexDirection: 'row', gap: 10, alignItems: 'flex-start' },
  stepNum: { color: G.greenInk, backgroundColor: G.green, width: 22, height: 22, borderRadius: 11, textAlign: 'center', fontWeight: '800', fontSize: 13, lineHeight: 22, overflow: 'hidden' },
  badge: { flexDirection: 'row', alignItems: 'center', gap: 6, borderWidth: 1, borderRadius: 999, paddingVertical: 4, paddingHorizontal: 10 },
  dot: { width: 8, height: 8, borderRadius: 4 },
  badgeText: { fontSize: 12, fontWeight: '800', letterSpacing: 0.5 },
});

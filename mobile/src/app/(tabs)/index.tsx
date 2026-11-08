import { router } from 'expo-router';
import { Platform, ScrollView, StyleSheet, Text, useWindowDimensions, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { Btn } from '@/components/games/ui';
import { BottomTabInset } from '@/constants/theme';
import { G } from '@/games/theme';

const FEATURES = [
  {
    title: 'Reps that count',
    body: "Half reps and rushed reps don't count, and you find out why the moment it happens.",
  },
  {
    title: 'Coaching during the set',
    body: 'Short cues when your depth drops or one side falls behind. Nothing to read mid-rep.',
  },
  {
    title: 'A report when you finish',
    body: 'A plain-language recap of what went well and the one thing to work on next time.',
  },
];

export default function HomeScreen() {
  const { width } = useWindowDimensions();
  const insets = useSafeAreaInsets();
  const wide = width >= 900;

  return (
    <ScrollView
      style={s.page}
      contentContainerStyle={[
        s.pageInner,
        // The web nav bar floats over the page; native tabs sit at the bottom.
        { paddingTop: Platform.OS === 'web' ? 112 : insets.top + 32, paddingBottom: insets.bottom + BottomTabInset + 48 },
      ]}>
      <View style={s.column}>
        <View style={[s.hero, wide && s.heroWide]}>
          <View style={[s.heroText, wide && { flex: 1 }]}>
            <Text style={s.eyebrow}>AI strength coach</Text>
            <Text style={[s.h1, !wide && s.h1Narrow]} accessibilityRole="header">
              Every rep counted.{'\n'}Every set coached.
            </Text>
            <Text style={s.lede}>
              SharkByte follows your squats and push-ups through your camera, counts only the reps you finish, and tells
              you what to fix while you train.
            </Text>
            <View style={s.ctas}>
              <Btn label="Start a workout" onPress={() => router.push('/pose')} testID="home-exercises" />
              <Btn label="Play GRIT Games" variant="ghost" onPress={() => router.push('/games')} testID="home-games" />
            </View>
            <Text style={s.fine}>Runs in your browser. Your video never leaves your device.</Text>
          </View>
          <WorkoutPreview style={wide ? { flex: 1, maxWidth: 440 } : undefined} />
        </View>

        <View style={s.divider} />

        <View style={[s.features, wide && s.featuresWide]}>
          {FEATURES.map((f, i) => (
            <View key={f.title} style={[s.feature, wide && { flex: 1 }]}>
              <Text style={s.featureNum}>{String(i + 1).padStart(2, '0')}</Text>
              <Text style={s.featureTitle}>{f.title}</Text>
              <Text style={s.featureBody}>{f.body}</Text>
            </View>
          ))}
        </View>
      </View>
    </ScrollView>
  );
}

/** A static example of the live workout view, in place of a hero image. */
function WorkoutPreview({ style }: { style?: object }) {
  return (
    <View style={[s.preview, style]} accessibilityLabel="Example of a live workout">
      <View style={s.previewTop}>
        <Text style={s.previewTitle}>Squat</Text>
        <View style={s.live}>
          <View style={s.liveDot} />
          <Text style={s.liveText}>Tracking</Text>
        </View>
      </View>
      <View>
        <Text style={s.statLabel}>Reps</Text>
        <Text style={s.bigNumber}>12</Text>
      </View>
      <View style={s.statsRow}>
        <PreviewStat label="Deepest" value="88°" />
        <PreviewStat label="Tempo" value="2.1 s" />
        <PreviewStat label="Not counted" value="1" />
      </View>
      <View style={s.cue}>
        <Text style={s.cueLabel}>Coach</Text>
        <Text style={s.cueText}>Your last two reps were shallower. Sit a little deeper.</Text>
      </View>
      <Text style={s.previewCaption}>Example</Text>
    </View>
  );
}

function PreviewStat({ label, value }: { label: string; value: string }) {
  return (
    <View style={{ flex: 1, gap: 2 }}>
      <Text style={s.statLabel}>{label}</Text>
      <Text style={s.statValue}>{value}</Text>
    </View>
  );
}

const s = StyleSheet.create({
  page: { flex: 1, backgroundColor: G.bg },
  pageInner: { alignItems: 'center', paddingHorizontal: 20 },
  column: { width: '100%', maxWidth: 1120, gap: 56 },

  hero: { gap: 40 },
  heroWide: { flexDirection: 'row', alignItems: 'center', gap: 64 },
  heroText: { gap: 20 },
  eyebrow: { color: G.muted, fontSize: 14, fontWeight: '600' },
  h1: { color: G.text, fontSize: 60, lineHeight: 64, fontWeight: '800', letterSpacing: -2 },
  h1Narrow: { fontSize: 40, lineHeight: 44, letterSpacing: -1.2 },
  lede: { color: G.muted, fontSize: 18, lineHeight: 28, maxWidth: 520 },
  ctas: { flexDirection: 'row', flexWrap: 'wrap', gap: 12, marginTop: 4 },
  fine: { color: G.muted, fontSize: 13, opacity: 0.8 },

  preview: { backgroundColor: G.panel, borderColor: G.line, borderWidth: 1, borderRadius: 20, padding: 24, gap: 20 },
  previewTop: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  previewTitle: { color: G.text, fontSize: 16, fontWeight: '700' },
  live: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  liveDot: { width: 7, height: 7, borderRadius: 4, backgroundColor: G.green },
  liveText: { color: G.muted, fontSize: 13, fontWeight: '600' },
  statLabel: { color: G.muted, fontSize: 12, fontWeight: '600' },
  bigNumber: { color: G.text, fontSize: 72, lineHeight: 76, fontWeight: '800', letterSpacing: -2, fontVariant: ['tabular-nums'] },
  statsRow: { flexDirection: 'row', gap: 12, borderTopWidth: 1, borderTopColor: G.line, paddingTop: 16 },
  statValue: { color: G.text, fontSize: 20, fontWeight: '700', fontVariant: ['tabular-nums'] },
  cue: { backgroundColor: G.panelHi, borderRadius: 12, padding: 14, gap: 4 },
  cueLabel: { color: G.green, fontSize: 12, fontWeight: '700' },
  cueText: { color: G.text, fontSize: 15, lineHeight: 21 },
  previewCaption: { color: G.muted, fontSize: 11, opacity: 0.7, alignSelf: 'flex-end', marginTop: -8 },

  divider: { height: 1, backgroundColor: G.line },
  features: { gap: 32 },
  featuresWide: { flexDirection: 'row', gap: 48 },
  feature: { gap: 8 },
  featureNum: { color: G.muted, fontSize: 13, fontWeight: '600', fontVariant: ['tabular-nums'] },
  featureTitle: { color: G.text, fontSize: 18, fontWeight: '700' },
  featureBody: { color: G.muted, fontSize: 15, lineHeight: 23 },
});

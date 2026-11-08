import { router } from 'expo-router';
import type { ReactNode } from 'react';
import { Pressable, Text, View } from 'react-native';

import { G } from '@/games/theme';

import { Badge, Body, Btn, GameScreen } from './ui';

function RacerPreview() {
  const lane = (top: number, p: number, c: string, icon: string, o = 1) => (
    <View style={{ position: 'absolute', left: 10, right: 10, top, height: 36, borderRadius: 8, backgroundColor: '#0b0f10', opacity: o, justifyContent: 'center' }}>
      <View style={{ position: 'absolute', left: 0, width: `${p}%`, top: 0, bottom: 0, backgroundColor: c, opacity: 0.18, borderRadius: 8 }} />
      <Text style={{ position: 'absolute', left: `${p}%`, fontSize: 22 }}>{icon}</Text>
      <View style={{ position: 'absolute', right: 0, width: 10, top: 0, bottom: 0, backgroundColor: '#fff', opacity: 0.85 }} />
    </View>
  );
  return (
    <View style={{ height: 150, borderRadius: 12, backgroundColor: '#0f1a14', overflow: 'hidden' }} accessibilityLabel="Rep Racer preview">
      {lane(20, 58, G.green, '🏃')}
      {lane(62, 40, G.blue, '👻', 0.6)}
      {lane(104, 24, G.amber, '🏃', 0.8)}
    </View>
  );
}

function GameCard({ title, tagline, bullets, preview, onPlay, testID }: { title: string; tagline: string; bullets: string[]; preview: ReactNode; onPlay: () => void; testID: string }) {
  return (
    <View style={{ flex: 1, minWidth: 300, maxWidth: 560, backgroundColor: G.panel, borderColor: G.line, borderWidth: 1, borderRadius: 18, padding: 16, gap: 12 }} testID={testID}>
      {preview}
      <View style={{ gap: 4 }}>
        <Text style={{ color: G.text, fontSize: 26, fontWeight: '900' }}>{title}</Text>
        <Text style={{ color: G.green, fontSize: 15, fontWeight: '700' }}>{tagline}</Text>
      </View>
      <View style={{ gap: 4 }}>
        {bullets.map((b) => (
          <Body key={b} style={{ color: G.muted }}>
            • {b}
          </Body>
        ))}
      </View>
      <Btn label={`Play ${title}`} onPress={onPlay} testID={`${testID}-play`} />
    </View>
  );
}

export function GamesHome() {
  return (
    <GameScreen title="GRIT Games" subtitle="Train. Play. Compete." back="/">
      <View style={{ flexDirection: 'row', gap: 10, flexWrap: 'wrap' }}>
        <Badge label="Camera-tracked reps are verified and saved" tone="green" />
        <Badge label="Your video never leaves your device" tone="blue" />
      </View>
      <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 16 }}>
        <GameCard
          testID="game-card-racer"
          title="Rep Racer"
          tagline="Race your friends or beat your personal best."
          preview={<RacerPreview />}
          bullets={['Solo goal: hit your target before the clock runs out.', 'Live 1v1: race a friend from separate laptops with a room code.', 'Personal best: chase a ghost of your own previous attempt.']}
          onPlay={() => router.push('/games/racer')}
        />
      </View>
      <Pressable onPress={() => router.push('/pose')} accessibilityRole="link">
        <Text style={{ color: G.muted, fontSize: 13 }}>Developer tools: open the pose lab →</Text>
      </Pressable>
    </GameScreen>
  );
}

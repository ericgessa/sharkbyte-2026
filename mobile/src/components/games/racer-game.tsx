import { useLocalSearchParams } from 'expo-router';
import { useState } from 'react';
import { Pressable, Text, View } from 'react-native';

import { GG as G } from '@/games/theme';

import { LocalRacer } from './racer-local';
import { MultiplayerRacer } from './racer-multiplayer';
import { SoloRacer } from './racer-solo';
import { Body, GameScreen } from './ui-grits';

type Mode = 'menu' | 'solo' | 'multiplayer' | 'personal_best' | 'local';

const MODES: { id: Exclude<Mode, 'menu'>; title: string; tag: string; text: string; icon: string }[] = [
  { id: 'solo', title: 'Solo Goal', tag: 'A', icon: '🎯', text: 'Pick an exercise, a target and a time limit. Reach the finish line before the clock runs out.' },
  { id: 'multiplayer', title: 'Live 1v1', tag: 'B', icon: '⚔️', text: 'Create a room, share the code, and race a friend in real time from your own webcams.' },
  { id: 'personal_best', title: 'Personal Best', tag: 'C', icon: '👻', text: 'Race a ghost of your own best recorded attempt and try to beat your time.' },
  { id: 'local', title: 'Local 2-Player', tag: 'D', icon: '👥', text: 'Two people, one camera, one computer: stand side by side and race each other. No second device needed.' },
];

export function RacerGame() {
  // /games/racer?mode=solo (etc.) opens straight into that mode: the GRIT Games cards link here.
  const { mode: startMode } = useLocalSearchParams<{ mode?: string }>();
  const [mode, setMode] = useState<Mode>(() => (MODES.some((m) => m.id === startMode) ? (startMode as Mode) : 'menu'));
  return (
    <GameScreen title="Rep Racer" subtitle="Race your friends or beat your personal best.">
      {mode === 'menu' ? (
        <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 14 }}>
          {MODES.map((m) => (
            <Pressable
              key={m.id}
              accessibilityRole="button"
              onPress={() => setMode(m.id)}
              testID={`racer-mode-${m.id}`}
              style={({ pressed }) => ({ flex: 1, minWidth: 260, backgroundColor: G.panel, borderRadius: 22, padding: 20, gap: 8, transform: [{ scale: pressed ? 0.985 : 1 }] })}>
              <Text style={{ fontSize: 34 }}>{m.icon}</Text>
              <Text style={{ color: G.green, fontSize: 12, fontWeight: '900', letterSpacing: 1.6 }}>MODE {m.tag}</Text>
              <Text style={{ color: G.text, fontSize: 24, fontWeight: '900', letterSpacing: -0.4 }}>{m.title}</Text>
              <Body style={{ color: G.muted }}>{m.text}</Body>
            </Pressable>
          ))}
        </View>
      ) : mode === 'local' ? (
        <LocalRacer onBack={() => setMode('menu')} />
      ) : mode === 'multiplayer' ? (
        <MultiplayerRacer onBack={() => setMode('menu')} />
      ) : (
        <SoloRacer key={mode} mode={mode} onBack={() => setMode('menu')} />
      )}
    </GameScreen>
  );
}

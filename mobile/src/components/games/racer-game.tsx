import { useState } from 'react';
import { Pressable, Text, View } from 'react-native';

import { G } from '@/games/theme';

import { MultiplayerRacer } from './racer-multiplayer';
import { SoloRacer } from './racer-solo';
import { Body, GameScreen } from './ui';

type Mode = 'menu' | 'solo' | 'multiplayer' | 'personal_best';

const MODES: { id: Exclude<Mode, 'menu'>; title: string; tag: string; text: string; icon: string }[] = [
  { id: 'solo', title: 'Solo Goal', tag: 'A', icon: '🎯', text: 'Pick an exercise, a target and a time limit. Reach the finish line before the clock runs out.' },
  { id: 'multiplayer', title: 'Live 1v1', tag: 'B', icon: '⚔️', text: 'Create a room, share the code, and race a friend in real time from your own webcams.' },
  { id: 'personal_best', title: 'Personal Best', tag: 'C', icon: '👻', text: 'Race a ghost of your own best recorded attempt and try to beat your time.' },
];

export function RacerGame() {
  const [mode, setMode] = useState<Mode>('menu');
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
              style={({ pressed }) => ({ flex: 1, minWidth: 260, backgroundColor: G.panel, borderColor: G.line, borderWidth: 1, borderRadius: 16, padding: 18, gap: 8, opacity: pressed ? 0.8 : 1 })}>
              <Text style={{ fontSize: 34 }}>{m.icon}</Text>
              <Text style={{ color: G.green, fontSize: 12, fontWeight: '800', letterSpacing: 2 }}>MODE {m.tag}</Text>
              <Text style={{ color: G.text, fontSize: 22, fontWeight: '800' }}>{m.title}</Text>
              <Body style={{ color: G.muted }}>{m.text}</Body>
            </Pressable>
          ))}
        </View>
      ) : mode === 'multiplayer' ? (
        <MultiplayerRacer onBack={() => setMode('menu')} />
      ) : (
        <SoloRacer key={mode} mode={mode} onBack={() => setMode('menu')} />
      )}
    </GameScreen>
  );
}

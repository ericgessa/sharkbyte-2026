import { router, type Href } from 'expo-router';
import { StyleSheet, View } from 'react-native';

import { GameButton, GameCard, GameScreen, GameText } from '@/components/game-ui';

/**
 * GRIT Games (games mockup): one card per way to play. The cards open Rep Racer straight into a mode
 * (/games/racer?mode=...), and the last one is Challenges (your best + 1 in 3:00). Every rep is counted by the server.
 */
export function GamesHome() {
  const racer = (mode: string) => router.push(`/games/racer?mode=${mode}` as Href);
  return (
    <GameScreen title="Games">
      <GameText muted size={15}>
        Camera-tracked: only full reps count. Your video never leaves your device.
      </GameText>

      <GameCard index={0} kicker="GAME 1" title="Rep Race" onPress={() => racer('solo')} testID="games-solo">
        <GameText muted>Pick a target and a time limit. Hit it before the clock runs out.</GameText>
      </GameCard>

      <GameCard index={1} kicker="GAME 2" title="Ghost Race" onPress={() => racer('personal_best')} testID="games-ghost">
        <GameText muted>Race the ghost of your own best attempt. Beat your time.</GameText>
      </GameCard>

      <GameCard index={2} kicker="GAME 3" title="Head to Head" testID="games-versus">
        <GameText muted>Race a friend, rep for rep.</GameText>
        <View style={styles.row}>
          <GameButton label="Online, with a code" onPress={() => racer('multiplayer')} testID="games-online" />
          <GameButton label="Same screen" tone="ghost" onPress={() => racer('local')} testID="games-local" />
        </View>
      </GameCard>

      <GameCard index={3} kicker="DAILY" title="Challenges" onPress={() => router.push('/challenges' as Href)} testID="games-challenges">
        <GameText muted>Your best + 1, in 3:00. Beat it and it goes on your record.</GameText>
      </GameCard>
    </GameScreen>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: 'row', flexWrap: 'wrap', gap: 10, marginTop: 6 },
});

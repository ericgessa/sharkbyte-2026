import { router, type Href } from 'expo-router';
import { useEffect, useState } from 'react';
import { StyleSheet, Text, View } from 'react-native';

import { GameButton, GameCard, GameScreen, GameText } from '@/components/game-ui';
import { Grits } from '@/constants/grits';
import { EXERCISES } from '@/exercise/catalog';
import { CHALLENGE_TIME_S, challengeFor, challengeRecord, isChallengeResult } from '@/exercise/challenges';
import { formatDuration } from '@/exercise/summary';
import { listGameResults, type GameResult } from '@/games/games-api';
import { getStats, type ExerciseStats } from '@/pose/workout-client';
import { getApiUrl } from '@/utils/api-url';
import { getAccessToken } from '@/utils/auth';

type Loaded = { stats: Record<string, ExerciseStats>; results: GameResult[]; recordOk: boolean } | 'loading' | 'error';

/** Challenges hub (games look): per exercise, your best + 1 in 3:00, your record, and the recent tries. */
export function ChallengesScreen() {
  const [data, setData] = useState<Loaded>('loading');

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const api = getApiUrl();
        const token = await getAccessToken();
        const [s, results] = await Promise.all([
          getStats(api, token),
          // The record is optional: if the games tables aren't set up, challenges still work, just unrecorded.
          listGameResults(api, token, 'rep_racer', 100).then(
            (r) => ({ ok: true, r }),
            () => ({ ok: false, r: [] as GameResult[] }),
          ),
        ]);
        if (!alive) return;
        const stats: Record<string, ExerciseStats> = {};
        for (const e of s.per_exercise) stats[e.exercise] = e;
        setData({ stats, results: results.r, recordOk: results.ok });
      } catch {
        if (alive) setData('error');
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const recent = data !== 'loading' && data !== 'error' ? data.results.filter(isChallengeResult).slice(0, 6) : [];

  return (
    <GameScreen title="Challenges" back="Games" backTo={'/games' as Href}>
      <GameText muted size={15}>
        Your best set + 1, in {formatDuration(CHALLENGE_TIME_S * 1000)}. The clock ends the set for you.
      </GameText>

      {data === 'loading' && <GameText muted>Loading your numbers…</GameText>}
      {data === 'error' && <GameText muted>Your numbers aren&apos;t available right now. Try again in a moment.</GameText>}

      {data !== 'loading' &&
        data !== 'error' &&
        EXERCISES.map((e, i) => {
          const c = challengeFor(e.slug, data.stats[e.slug]);
          const rec = challengeRecord(data.results, e.slug);
          const noun = e.label.toLowerCase();
          return (
            <GameCard key={e.slug} index={i} kicker={c.locked ? 'LOCKED' : rec.streak >= 2 ? `${rec.streak} WINS IN A ROW` : 'READY'} title={e.label} dim={c.locked} testID={`challenge-${e.slug}`}>
              {c.locked ? (
                <>
                  <GameText muted>Do one set of {noun} first: the challenge is built from your best.</GameText>
                  <View style={styles.row}>
                    <GameButton label="Go do a set" tone="ghost" onPress={() => router.push(`/exercise/${e.slug}` as Href)} testID={`challenge-${e.slug}-unlock`} />
                  </View>
                </>
              ) : (
                <>
                  <View style={styles.targetRow}>
                    <Text style={styles.target}>{c.target}</Text>
                    <GameText muted style={{ flex: 1 }}>
                      {noun} in {formatDuration(CHALLENGE_TIME_S * 1000)}
                      {'\n'}your best is {c.best}
                    </GameText>
                  </View>
                  {c.recorded && data.recordOk ? (
                    <GameText muted size={14}>
                      {rec.tries === 0 ? 'No tries yet.' : `Won ${rec.wins} of ${rec.tries} ${rec.tries === 1 ? 'try' : 'tries'}.`}
                    </GameText>
                  ) : (
                    <GameText muted size={14}>
                      {c.recorded ? 'Your record isn’t available right now.' : 'Played for fun: wins aren’t recorded for this one yet.'}
                    </GameText>
                  )}
                  <View style={styles.row}>
                    <GameButton label="Take it on" onPress={() => router.push(`/exercise/session/${e.slug}?challenge=${c.target}` as Href)} testID={`challenge-${e.slug}-start`} />
                  </View>
                </>
              )}
            </GameCard>
          );
        })}

      {recent.length > 0 && (
        <GameCard index={EXERCISES.length} kicker="RECENT" title="Your last tries">
          {recent.map((r) => (
            <View key={r.id} style={styles.tryRow}>
              <View style={[styles.dot, { backgroundColor: r.completed ? Grits.sageDeep : Grits.orange }]} />
              <GameText size={15} style={{ flex: 1 }}>
                {r.valid_reps}/{r.target_reps} {r.exercise === 'push-up' ? 'push-ups' : 'squats'}
              </GameText>
              <GameText muted size={14}>
                {r.completed ? 'won' : 'missed'} · {new Date(r.created_at).toLocaleDateString()}
              </GameText>
            </View>
          ))}
        </GameCard>
      )}
    </GameScreen>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: 'row', flexWrap: 'wrap', gap: 10, marginTop: 4 },
  targetRow: { flexDirection: 'row', alignItems: 'center', gap: 14 },
  target: { color: Grits.orange, fontSize: 64, fontWeight: '900', letterSpacing: -2, includeFontPadding: false },
  tryRow: { flexDirection: 'row', alignItems: 'center', gap: 10, paddingVertical: 4 },
  dot: { width: 12, height: 12, borderRadius: 6 },
});

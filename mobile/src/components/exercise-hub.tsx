import { router, type Href } from 'expo-router';
import { useEffect, useState } from 'react';
import { Pressable, StyleSheet, Text, useWindowDimensions, View } from 'react-native';

import { GRITS_MAX_WIDTH, GritsHeader, GritsScreen, Ink, Plate } from '@/components/grits-ui';
import { Grits } from '@/constants/grits';
import { EXERCISES, type ExerciseInfo } from '@/exercise/catalog';
import { getStats, type ExerciseStats } from '@/pose/workout-client';
import { getApiUrl } from '@/utils/api-url';
import { getAccessToken } from '@/utils/auth';

type Loaded = { state: 'loading' } | { state: 'ok'; byExercise: Record<string, ExerciseStats> } | { state: 'error' };

/**
 * Pick an exercise. Each plate carries what you've done with it so far (best set, total reps, sessions);
 * a new user sees "New" on all three. Tapping one opens its screen (Start / Trends / Challenge).
 */
export function ExerciseHub() {
  const { width } = useWindowDimensions();
  const column = Math.min(width, GRITS_MAX_WIDTH) - 44;
  const plate = Math.min(column * 0.66, 300);
  const [stats, setStats] = useState<Loaded>({ state: 'loading' });

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const s = await getStats(getApiUrl(), await getAccessToken());
        if (!alive) return;
        const byExercise: Record<string, ExerciseStats> = {};
        for (const e of s.per_exercise) byExercise[e.exercise] = e;
        setStats({ state: 'ok', byExercise });
      } catch {
        if (alive) setStats({ state: 'error' }); // the plates still work; they just can't show history
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const open = (e: ExerciseInfo) => router.push(`/exercise/${e.slug}` as Href);

  return (
    <GritsScreen bubbles="hub">
      <GritsHeader title="Exercise" back="Home" />
      <Ink soft size={16} style={styles.lede}>
        Pick one. Go until you&apos;re tired. Every set is logged.
      </Ink>

      <View style={styles.plates}>
        {EXERCISES.map((e, i) => (
          <ExercisePlate key={e.slug} info={e} size={plate} align={i % 2 === 0 ? 'flex-start' : 'flex-end'} stats={stats} onPress={() => open(e)} />
        ))}
      </View>

    </GritsScreen>
  );
}

function ExercisePlate({ info, size, align, stats, onPress }: { info: ExerciseInfo; size: number; align: 'flex-start' | 'flex-end'; stats: Loaded; onPress: () => void }) {
  const s = stats.state === 'ok' ? stats.byExercise[info.slug] : undefined;
  const isNew = stats.state === 'ok' && (!s || s.workouts === 0);
  const big = stats.state === 'loading' ? '…' : s && s.workouts > 0 ? String(s.best_workout_reps) : isNew ? 'New' : '–';
  const small =
    stats.state === 'loading'
      ? ' '
      : s && s.workouts > 0
        ? `best set · ${s.total_reps} total · ${s.workouts} ${s.workouts === 1 ? 'set' : 'sets'}`
        : isNew
          ? 'tap to start'
          : 'history unavailable';

  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`${info.label}. ${small === ' ' ? '' : `${big} ${small}`}`}
      onPress={onPress}
      testID={`hub-${info.slug}`}
      style={({ pressed }) => [{ alignSelf: align }, pressed && styles.platePressed]}>
      <Plate size={size}>
        <Text style={[styles.plateLabel, { fontSize: size * 0.085 }]}>{info.label}</Text>
        <Text style={[styles.plateBig, { fontSize: size * (big.length > 3 ? 0.2 : 0.26) }]}>{big}</Text>
        <Text style={[styles.plateSmall, { fontSize: Math.max(11, size * 0.048), maxWidth: size * 0.7 }]}>{small}</Text>
      </Plate>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  lede: { marginBottom: 8 },
  plates: { gap: 6, marginVertical: 8 },
  platePressed: { transform: [{ scale: 0.97 }] },
  plateLabel: { color: Grits.ink, fontWeight: '800' },
  plateBig: { color: Grits.ink, fontWeight: '900', letterSpacing: -1, includeFontPadding: false },
  plateSmall: { color: Grits.ink, fontWeight: '600', textAlign: 'center', opacity: 0.85 },
});

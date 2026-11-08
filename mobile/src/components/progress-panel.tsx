import { router, useFocusEffect } from 'expo-router';
import { useCallback, useState } from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';

import { G } from '@/games/theme';
import {
  compactNumber,
  exerciseNoun,
  fastestRace,
  formatDuration,
  getProgress,
  shortWeekLabel,
  streakHint,
  type Progress,
  type WeekActivity,
} from '@/pose/progress';
import { getApiUrl } from '@/utils/api-url';
import { getAccessToken } from '@/utils/auth';

const CHART_H = 96;
const DIM = '#5d6b67'; // past weeks: de-emphasised, so this week (green) is the one that stands out

/**
 * The signed-in user's progress: streak, totals, best set and 8 weeks of activity, from GET /api/stats.
 * Reloads whenever the screen comes back into focus (e.g. after finishing a workout).
 */
export function ProgressPanel({ wide }: { wide: boolean }) {
  const [data, setData] = useState<Progress | null>(null);
  const [failed, setFailed] = useState(false);

  const load = useCallback(() => {
    let cancelled = false;
    (async () => {
      try {
        const p = await getProgress(getApiUrl(), await getAccessToken());
        if (!cancelled) {
          setData(p);
          setFailed(false);
        }
      } catch {
        if (!cancelled) setFailed(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);
  useFocusEffect(load);

  return (
    <View style={s.section} testID="progress">
      <Text style={s.heading}>Your progress</Text>
      {!data ? (
        <View style={[s.card, s.center]}>
          {failed ? (
            <>
              <Text style={s.muted}>Couldn&apos;t load your progress.</Text>
              <Pressable onPress={load} accessibilityRole="button" style={s.retry}>
                <Text style={s.retryText}>Try again</Text>
              </Pressable>
            </>
          ) : (
            <ActivityIndicator color={G.muted} />
          )}
        </View>
      ) : (
        <View style={[s.grid, wide && s.gridWide]}>
          <StreakCard p={data} style={wide ? { flex: 1 } : undefined} />
          <View style={[s.card, wide && { flex: 2 }]}>
            <View style={s.tiles}>
              <Tile label="Workouts" value={compactNumber(data.totals.workouts)} />
              <Tile label="Reps" value={compactNumber(data.totals.reps)} />
              <Tile label="Time trained" value={formatDuration(data.totals.active_seconds)} />
            </View>
            <WeeklyChart weeks={data.weekly} />
            <Records p={data} />
          </View>
        </View>
      )}
    </View>
  );
}

function StreakCard({ p, style }: { p: Progress; style?: object }) {
  const days = p.streak.current_days;
  return (
    <View style={[s.card, s.streak, style]} accessibilityLabel={`Current streak ${days} ${days === 1 ? 'day' : 'days'}. Best ${p.streak.longest_days}.`}>
      <Text style={s.tileLabel}>Current streak</Text>
      <View style={s.streakRow}>
        <Text style={s.hero}>{days}</Text>
        <Text style={s.heroUnit}>{days === 1 ? 'day' : 'days'}</Text>
      </View>
      <Text style={s.body}>{streakHint(p)}</Text>
      <View style={s.rule} />
      <Text style={s.muted}>
        Best streak <Text style={s.strong}>{p.streak.longest_days} {p.streak.longest_days === 1 ? 'day' : 'days'}</Text>
      </Text>
    </View>
  );
}

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <View style={s.tile}>
      <Text style={s.tileLabel}>{label}</Text>
      <Text style={s.tileValue}>{value}</Text>
    </View>
  );
}

/** Workouts per week, oldest to newest. This week is green; hover or tap a column for its numbers. */
function WeeklyChart({ weeks }: { weeks: WeekActivity[] }) {
  const current = weeks.length - 1;
  const [active, setActive] = useState(current);
  if (weeks.length === 0) return null;
  const max = Math.max(1, ...weeks.map((w) => w.workouts));
  const shown = weeks[Math.min(active, current)];
  const summary = weeks.map((w) => `week of ${shortWeekLabel(w.week_start)}: ${w.workouts} workouts`).join(', ');

  return (
    <View style={s.chartBlock}>
      <View style={s.chartHead}>
        <Text style={s.tileLabel}>Workouts per week</Text>
        <Text style={s.readout} testID="progress-week-readout">
          {active === current ? 'This week' : `Week of ${shortWeekLabel(shown.week_start)}`}
          {'  '}
          <Text style={s.strong}>
            {shown.workouts} {shown.workouts === 1 ? 'workout' : 'workouts'} · {compactNumber(shown.reps)} reps
          </Text>
        </Text>
      </View>
      <View style={s.chart} accessibilityLabel={`Workouts per week, last ${weeks.length} weeks: ${summary}`} onPointerLeave={() => setActive(current)}>
        {weeks.map((w, i) => {
          const h = w.workouts === 0 ? 2 : Math.max(6, (w.workouts / max) * CHART_H);
          return (
            <Pressable
              key={w.week_start}
              style={s.col}
              onHoverIn={() => setActive(i)}
              onPress={() => setActive(i)}
              accessibilityRole="button"
              accessibilityLabel={`Week of ${shortWeekLabel(w.week_start)}: ${w.workouts} workouts, ${w.reps} reps`}>
              <View style={[s.bar, { height: h, backgroundColor: i === current ? G.green : DIM, opacity: active === i || i === current ? 1 : 0.75 }]} />
            </Pressable>
          );
        })}
      </View>
      <View style={s.axis}>
        <Text style={s.axisLabel}>{shortWeekLabel(weeks[0].week_start)}</Text>
        <Text style={s.axisLabel}>This week</Text>
      </View>
    </View>
  );
}

function Records({ p }: { p: Progress }) {
  const best = p.records.most_reps_in_a_workout;
  const race = fastestRace(p.games);
  if (!best && !race) return null;
  return (
    <View style={s.records}>
      {best && (
        <Pressable
          disabled={!best.workout_id}
          onPress={() => best.workout_id && router.push({ pathname: '/workout/[id]', params: { id: best.workout_id } })}
          accessibilityRole={best.workout_id ? 'link' : undefined}>
          <Text style={s.muted}>
            Best set <Text style={s.strong}>{best.reps} {exerciseNoun(best.exercise, best.reps)}</Text>
            {best.workout_id ? <Text style={s.link}>  View</Text> : null}
          </Text>
        </Pressable>
      )}
      {race && (
        <Text style={s.muted}>
          Fastest Rep Racer <Text style={s.strong}>{race}</Text>
        </Text>
      )}
    </View>
  );
}

const s = StyleSheet.create({
  section: { gap: 16 },
  heading: { color: G.text, fontSize: 22, fontWeight: '800', letterSpacing: -0.4 },
  grid: { gap: 16 },
  gridWide: { flexDirection: 'row', alignItems: 'stretch' },
  card: { backgroundColor: G.panel, borderColor: G.line, borderWidth: 1, borderRadius: 16, padding: 20, gap: 18 },
  center: { alignItems: 'center', justifyContent: 'center', minHeight: 120, gap: 10 },

  streak: { gap: 10 },
  streakRow: { flexDirection: 'row', alignItems: 'baseline', gap: 8 },
  hero: { color: G.text, fontSize: 64, lineHeight: 68, fontWeight: '800', letterSpacing: -2, fontVariant: ['tabular-nums'] },
  heroUnit: { color: G.muted, fontSize: 18, fontWeight: '600' },
  rule: { height: 1, backgroundColor: G.line, marginVertical: 4 },

  tiles: { flexDirection: 'row', gap: 12 },
  tile: { flex: 1, gap: 4 },
  tileLabel: { color: G.muted, fontSize: 12, fontWeight: '600' },
  tileValue: { color: G.text, fontSize: 26, fontWeight: '700', fontVariant: ['tabular-nums'] },

  chartBlock: { gap: 10 },
  chartHead: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'baseline', flexWrap: 'wrap', gap: 6 },
  readout: { color: G.muted, fontSize: 13 },
  chart: { height: CHART_H, flexDirection: 'row', alignItems: 'flex-end', borderBottomWidth: 1, borderBottomColor: G.line },
  col: { flex: 1, height: '100%', alignItems: 'center', justifyContent: 'flex-end' },
  bar: { width: '60%', maxWidth: 24, borderTopLeftRadius: 4, borderTopRightRadius: 4 },
  axis: { flexDirection: 'row', justifyContent: 'space-between' },
  axisLabel: { color: G.muted, fontSize: 11 },

  records: { gap: 4, borderTopWidth: 1, borderTopColor: G.line, paddingTop: 14 },
  body: { color: G.text, fontSize: 15, lineHeight: 22 },
  muted: { color: G.muted, fontSize: 14 },
  strong: { color: G.text, fontWeight: '700' },
  link: { color: G.green, fontWeight: '700' },
  retry: { borderWidth: 1, borderColor: G.line, borderRadius: 10, paddingVertical: 6, paddingHorizontal: 14 },
  retryText: { color: G.text, fontSize: 14, fontWeight: '700' },
});

import { useEffect, useMemo, useState } from 'react';
import { ActivityIndicator, StyleSheet, Text, View } from 'react-native';

import { G } from '@/games/theme';
import { bucketSecondsFor, buildChart, chartWindow, getReadings, metricsToFetch, type ChartData, type Joint } from '@/pose/readings';
import type { WorkoutDetail } from '@/pose/history';
import { getApiUrl } from '@/utils/api-url';
import { getAccessToken } from '@/utils/auth';

import { AngleChart, type ChartLine } from './angle-chart';

type State =
  | { status: 'loading' }
  | { status: 'unavailable' } // Tiger Data is off or down: a normal answer
  | { status: 'error' }
  | { status: 'ready'; raw: Awaited<ReturnType<typeof getReadings>>['readings'] };

const BACK_COLORS: Record<string, string> = { spine_lumbar: G.amber, spine_mid: G.blue, spine_upper: '#c792ea' };

/**
 * "Angle over time": the camera's joint angle through the whole workout, and the wearable's back angles, drawn from the
 * readings stored in Tiger Data. Each dip is a rep; the shaded bands are the reps the server counted.
 */
export function WorkoutCharts({ id, workout, reps, joint }: { id: string; workout: WorkoutDetail['workout']; reps: WorkoutDetail['reps']; joint: Joint }) {
  const win = useMemo(() => chartWindow(workout.started_at, workout.ended_at), [workout.started_at, workout.ended_at]);
  const [state, setState] = useState<State>({ status: 'loading' });

  useEffect(() => {
    if (!win) return;
    let cancelled = false;
    (async () => {
      try {
        const token = await getAccessToken();
        const r = await getReadings(getApiUrl(), token, id, { start: win.start, end: win.end, metrics: metricsToFetch(joint), bucketSeconds: bucketSecondsFor(win.durationS) });
        if (cancelled) return;
        setState(r.available ? { status: 'ready', raw: r.readings } : { status: 'unavailable' });
      } catch {
        if (!cancelled) setState({ status: 'error' });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [id, joint, win]);

  const chart: ChartData | null = useMemo(
    () => (state.status === 'ready' ? buildChart(state.raw, workout.started_at, workout.ended_at, joint, reps) : null),
    [state, workout.started_at, workout.ended_at, joint, reps],
  );

  if (!win) return null;

  return (
    <View style={s.section} testID="workout-charts">
      <Text style={s.sectionTitle}>Angle over time</Text>
      {state.status === 'loading' && (
        <View style={s.card}>
          <ActivityIndicator color={G.muted} />
        </View>
      )}
      {state.status === 'unavailable' && (
        <View style={s.card}>
          <Text style={s.muted}>The stored readings aren&apos;t available right now (the time-series database is off or unreachable).</Text>
        </View>
      )}
      {state.status === 'error' && (
        <View style={s.card}>
          <Text style={s.muted}>Couldn&apos;t load the readings for this workout.</Text>
        </View>
      )}
      {chart && !chart.angle && chart.back.length === 0 && (
        <View style={s.card} testID="workout-charts-empty">
          <Text style={s.muted}>No readings were stored for this workout.</Text>
        </View>
      )}
      {chart?.angle && (
        <View style={s.card}>
          <Text style={s.cardTitle}>{chart.joint === 'knee' ? 'Knee' : 'Elbow'} angle</Text>
          <Text style={s.caption}>Lower = deeper. Each dip is a rep; the shaded bands are the reps counted (numbered).</Text>
          <AngleChart lines={[{ ...chart.angle, color: G.green }]} repBands={chart.repBands} durationS={chart.durationS} testID="chart-angle" />
        </View>
      )}
      {chart && chart.back.length > 0 && (
        <View style={s.card}>
          <Text style={s.cardTitle}>Back shape (wearable)</Text>
          <Text style={s.caption}>Bend within each part of the spine, + = bending forward. A rounding lower back climbs.</Text>
          <AngleChart lines={chart.back.map<ChartLine>((b) => ({ ...b, color: BACK_COLORS[b.key] ?? G.muted }))} repBands={chart.repBands} durationS={chart.durationS} testID="chart-back" />
        </View>
      )}
      {chart && (chart.angle || chart.back.length > 0) && (
        <Text style={s.caption} testID="chart-source">
          Drawn from {chart.readings.toLocaleString()} readings stored in Tiger Data, averaged over {bucketSecondsFor(win.durationS)} s.
        </Text>
      )}
    </View>
  );
}

const s = StyleSheet.create({
  section: { gap: 10 },
  sectionTitle: { color: G.text, fontSize: 18, fontWeight: '700' },
  card: { backgroundColor: G.panel, borderColor: G.line, borderWidth: 1, borderRadius: 16, padding: 16, gap: 6 },
  cardTitle: { color: G.text, fontSize: 15, fontWeight: '700' },
  muted: { color: G.muted, fontSize: 14 },
  caption: { color: G.muted, fontSize: 12 },
});

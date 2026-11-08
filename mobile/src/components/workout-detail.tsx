import { router } from 'expo-router';
import { useEffect, useState } from 'react';
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { Btn } from '@/components/games/ui';
import { G } from '@/games/theme';
import { dayLabel, deleteWorkout, exerciseTitle, getWorkoutDetail, noteKind, timeLabel, workoutLength, type WorkoutDetail } from '@/pose/history';
import { getCoachReport, type CoachReport } from '@/pose/workout-client';
import { WorkoutCharts } from '@/components/workout-charts';
import { getApiUrl } from '@/utils/api-url';
import { getAccessToken } from '@/utils/auth';

/** One workout: stats, the coach report, the coaching notes in order, each rep, and delete. */
export function WorkoutDetailScreen({ id }: { id: string }) {
  const insets = useSafeAreaInsets();
  const [data, setData] = useState<WorkoutDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<CoachReport | 'loading' | 'failed' | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const token = await getAccessToken();
        const d = await getWorkoutDetail(getApiUrl(), token, id);
        if (cancelled) return;
        setData(d);
        if (d.workout.status !== 'completed') return; // a report for an open workout would go stale
        setReport('loading');
        try {
          const r = await getCoachReport(getApiUrl(), token, id);
          if (!cancelled) setReport(r);
        } catch {
          if (!cancelled) setReport('failed');
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error && e.message.startsWith('404') ? 'This workout no longer exists.' : "Couldn't load this workout.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [id]);

  if (!data) {
    return (
      <View style={[s.page, s.center]}>
        {error ? <Text style={s.muted}>{error}</Text> : <ActivityIndicator color={G.muted} />}
      </View>
    );
  }

  const w = data.workout;
  const joint = data.reps.find((r) => r.metrics.joint)?.metrics.joint ?? 'knee';
  const length = workoutLength(w.started_at, w.ended_at);
  const st = data.stats;

  return (
    <ScrollView style={s.page} contentContainerStyle={[s.pageInner, { paddingBottom: insets.bottom + 48 }]}>
      <View style={s.column}>
        <View style={{ gap: 4 }}>
          <Text style={s.h1} accessibilityRole="header">
            {exerciseTitle(w.exercise)}
          </Text>
          <Text style={s.muted}>{[`${dayLabel(w.started_at)}, ${timeLabel(w.started_at)}`, length, w.status === 'active' ? 'in progress' : null].filter(Boolean).join(' · ')}</Text>
        </View>

        {w.analysis_supported ? (
          <View style={s.tiles}>
            <Tile label="Reps" value={String(st.rep_count)} />
            <Tile label={`Deepest ${joint} angle`} value={deg(st.deepest_angle_deg)} />
            <Tile label="Average depth" value={deg(st.avg_min_angle_deg)} />
            <Tile label="Depth change" value={depthChange(st.depth_change_deg)} />
          </View>
        ) : (
          <View style={s.card}>
            <Text style={s.muted}>This exercise is logged only: reps aren&apos;t counted for it yet.</Text>
          </View>
        )}

        {report && (
          <View style={[s.card, s.reportCard]} testID="workout-report">
            <Text style={s.cardTitle}>Coach report</Text>
            <Text style={s.body}>
              {report === 'loading' ? 'Your coach is writing your report…' : report === 'failed' ? "The report couldn't be loaded right now." : report.summary}
            </Text>
            {typeof report === 'object' && <Text style={s.caption}>{report.source === 'agent' ? 'Written by Gemini' : 'Standard report'}</Text>}
          </View>
        )}

        {w.status === 'completed' && w.analysis_supported && <WorkoutCharts id={id} workout={w} reps={data.reps} joint={joint === 'elbow' ? 'elbow' : 'knee'} />}

        {data.coaching_events.length > 0 && (
          <View style={s.section}>
            <Text style={s.sectionTitle}>Coaching notes</Text>
            <View style={s.listCard}>
              {data.coaching_events.map((e, i) => {
                const kind = noteKind(e.action);
                return (
                  <View key={`${e.created_at}-${i}`} style={[s.note, i > 0 && s.divider]}>
                    <Text style={s.noteRep}>{e.rep_number ? `Rep ${e.rep_number}` : 'Start'}</Text>
                    <View style={{ flex: 1, gap: 2 }}>
                      <Text style={[s.noteKind, kind === 'not counted' && { color: G.amber }, kind === 'rest' && { color: G.red }]}>
                        {kind === 'not counted' ? "Didn't count" : kind === 'rest' ? 'Rest advised' : 'Cue'}
                      </Text>
                      <Text style={s.body}>{e.message}</Text>
                    </View>
                  </View>
                );
              })}
            </View>
          </View>
        )}

        {data.reps.length > 0 && (
          <View style={s.section}>
            <Text style={s.sectionTitle}>Reps</Text>
            <View style={s.listCard}>
              <View style={[s.tr, s.th]}>
                <Text style={[s.td, s.tdNum, s.thText]}>#</Text>
                <Text style={[s.td, s.thText]}>Deepest {joint} angle</Text>
                <Text style={[s.td, s.thText, s.right]}>Time</Text>
              </View>
              {data.reps.map((r) => (
                <View key={r.rep_number} style={[s.tr, s.divider]}>
                  <Text style={[s.td, s.tdNum, s.muted]}>{r.rep_number}</Text>
                  <Text style={[s.td, s.cell]}>{deg(r.metrics.min_angle_deg ?? null)}</Text>
                  <Text style={[s.td, s.cell, s.right]}>{r.metrics.duration_ms != null ? `${(r.metrics.duration_ms / 1000).toFixed(1)} s` : '–'}</Text>
                </View>
              ))}
            </View>
            <Text style={s.caption}>Smaller angle = deeper.</Text>
          </View>
        )}

        <DeleteWorkout id={id} />
      </View>
    </ScrollView>
  );
}

function DeleteWorkout({ id }: { id: string }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const remove = async () => {
    setBusy(true);
    setError(null);
    try {
      await deleteWorkout(getApiUrl(), await getAccessToken(), id);
      if (router.canGoBack()) router.back();
      else router.replace('/history');
    } catch (e) {
      const msg = e instanceof Error ? e.message : '';
      setError(msg.startsWith('503') ? 'Couldn’t delete right now. Nothing was removed; try again in a moment.' : 'Couldn’t delete this workout.');
      setBusy(false);
    }
  };

  return (
    <View style={s.danger}>
      {confirming ? (
        <>
          <Text style={s.body}>Delete this workout for good? Its reps, coaching notes and sensor data are removed too. This can&apos;t be undone.</Text>
          <View style={s.dangerRow}>
            <Btn label="Cancel" variant="ghost" onPress={() => setConfirming(false)} disabled={busy} />
            <Btn label={busy ? 'Deleting…' : 'Delete workout'} variant="danger" onPress={remove} disabled={busy} testID="workout-delete-confirm" />
          </View>
        </>
      ) : (
        <Pressable onPress={() => setConfirming(true)} accessibilityRole="button" testID="workout-delete">
          <Text style={s.dangerLink}>Delete workout</Text>
        </Pressable>
      )}
      {error && <Text style={[s.body, { color: G.red }]}>{error}</Text>}
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

const deg = (v: number | null | undefined) => (v == null ? '–' : `${Math.round(v)}°`);

/** Positive = later reps were shallower. */
function depthChange(v: number | null): string {
  if (v == null) return '–';
  if (Math.abs(v) < 3) return 'Steady';
  return `${Math.round(Math.abs(v))}° ${v > 0 ? 'shallower' : 'deeper'}`;
}

const s = StyleSheet.create({
  page: { flex: 1, backgroundColor: G.bg },
  center: { alignItems: 'center', justifyContent: 'center' },
  pageInner: { alignItems: 'center', paddingHorizontal: 20, paddingTop: 24 },
  column: { width: '100%', maxWidth: 760, gap: 24 },
  h1: { color: G.text, fontSize: 36, lineHeight: 40, fontWeight: '800', letterSpacing: -1 },
  muted: { color: G.muted, fontSize: 14 },
  body: { color: G.text, fontSize: 15, lineHeight: 22 },
  caption: { color: G.muted, fontSize: 12 },

  tiles: { flexDirection: 'row', flexWrap: 'wrap', gap: 12 },
  tile: { flexGrow: 1, flexBasis: 150, backgroundColor: G.panel, borderColor: G.line, borderWidth: 1, borderRadius: 14, padding: 16, gap: 4 },
  tileLabel: { color: G.muted, fontSize: 12, fontWeight: '600' },
  tileValue: { color: G.text, fontSize: 24, fontWeight: '700', fontVariant: ['tabular-nums'] },

  card: { backgroundColor: G.panel, borderColor: G.line, borderWidth: 1, borderRadius: 16, padding: 18, gap: 8 },
  reportCard: { borderLeftWidth: 3, borderLeftColor: G.green },
  cardTitle: { color: G.text, fontSize: 15, fontWeight: '700' },

  section: { gap: 10 },
  sectionTitle: { color: G.text, fontSize: 18, fontWeight: '700' },
  listCard: { backgroundColor: G.panel, borderColor: G.line, borderWidth: 1, borderRadius: 16, overflow: 'hidden' },
  divider: { borderTopWidth: 1, borderTopColor: G.line },
  note: { flexDirection: 'row', gap: 14, paddingVertical: 12, paddingHorizontal: 16 },
  noteRep: { color: G.muted, fontSize: 13, width: 48, paddingTop: 1, fontVariant: ['tabular-nums'] },
  noteKind: { color: G.muted, fontSize: 12, fontWeight: '700' },

  tr: { flexDirection: 'row', alignItems: 'center', paddingVertical: 10, paddingHorizontal: 16 },
  th: { backgroundColor: G.panelHi },
  thText: { color: G.muted, fontSize: 12, fontWeight: '600' },
  td: { flex: 1 },
  tdNum: { flex: 0, width: 40, fontVariant: ['tabular-nums'] },
  cell: { color: G.text, fontSize: 14, fontVariant: ['tabular-nums'] },
  right: { textAlign: 'right' },

  danger: { gap: 12, borderTopWidth: 1, borderTopColor: G.line, paddingTop: 20, marginTop: 8 },
  dangerRow: { flexDirection: 'row', gap: 10, flexWrap: 'wrap' },
  dangerLink: { color: G.red, fontSize: 14, fontWeight: '700' },
});

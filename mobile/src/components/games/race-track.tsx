import { useEffect, useRef, useState } from 'react';
import { StyleSheet, Text, View } from 'react-native';

import { GG as G } from '@/games/theme';

export type Lane = {
  id: string;
  label: string;
  /** 0..1 along the track (the real, server-counted progress). */
  progress: number;
  reps?: number;
  target?: number;
  color?: string;
  /** A replay of a previous attempt: drawn faint, with a label that says so. */
  ghost?: boolean;
  finished?: boolean;
};

/** Eases a value towards its target so a runner glides forward after each rep instead of teleporting. */
function useSmoothed(target: number, tauMs = 260): number {
  const [value, setValue] = useState(target);
  const targetRef = useRef(target);
  const valueRef = useRef(target);
  useEffect(() => {
    targetRef.current = target;
  }, [target]);
  useEffect(() => {
    let raf = 0;
    let last = performance.now();
    const step = (now: number) => {
      const dt = now - last;
      last = now;
      const next = valueRef.current + (targetRef.current - valueRef.current) * (1 - Math.exp(-dt / tauMs));
      valueRef.current = Math.abs(next - targetRef.current) < 0.0005 ? targetRef.current : next;
      setValue((prev) => (prev === valueRef.current ? prev : valueRef.current));
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [tauMs]);
  return value;
}

function Runner({ lane }: { lane: Lane }) {
  const shown = useSmoothed(lane.progress);
  const [bob, setBob] = useState(0);
  const moving = Math.abs(shown - lane.progress) > 0.002;
  useEffect(() => {
    if (!moving) return;
    const id = setInterval(() => setBob((b) => b + 1), 90);
    return () => clearInterval(id);
  }, [moving]);
  const color = lane.color ?? G.green;
  return (
    <View style={[s.lane, lane.ghost && { opacity: 0.55 }]} testID={`lane-${lane.id}`}>
      <View style={s.laneHead}>
        <Text style={[s.laneLabel, { color }]}>
          {lane.label}
          {lane.ghost ? ' · ghost' : ''}
        </Text>
        <Text style={s.laneReps} testID={`lane-${lane.id}-reps`}>
          {lane.reps ?? 0}
          {lane.target ? ` / ${lane.target}` : ''} reps
        </Text>
      </View>
      <View style={s.track}>
        <View style={s.dashes} />
        <View style={[s.fill, { width: `${Math.min(100, shown * 100)}%`, backgroundColor: color }]} />
        <View style={[s.runner, { left: `${Math.min(shown, 1) * 91}%`, top: 6 + (moving ? (bob % 2) * -3 : 0), borderColor: color }]} testID={`runner-${lane.id}`}>
          <Text style={s.runnerIcon}>{lane.ghost ? '👻' : lane.finished ? '🏆' : '🏃'}</Text>
        </View>
        <View style={s.finish}>
          {Array.from({ length: 8 }).map((_, i) => (
            <View key={i} style={{ flex: 1, flexDirection: 'row' }}>
              <View style={{ flex: 1, backgroundColor: i % 2 ? '#fff' : '#111' }} />
              <View style={{ flex: 1, backgroundColor: i % 2 ? '#111' : '#fff' }} />
            </View>
          ))}
        </View>
      </View>
      <Text style={s.pct}>{Math.round(Math.min(1, lane.progress) * 100)}%</Text>
    </View>
  );
}

export function RaceTrack({ lanes }: { lanes: Lane[] }) {
  return (
    <View style={{ gap: 10 }} testID="race-track">
      {lanes.map((l) => (
        <Runner key={l.id} lane={l} />
      ))}
    </View>
  );
}

const s = StyleSheet.create({
  lane: { backgroundColor: G.panel, borderRadius: 22, padding: 14, gap: 6 },
  laneHead: { flexDirection: 'row', justifyContent: 'space-between' },
  laneLabel: { fontSize: 14, fontWeight: '800', letterSpacing: 0.5 },
  laneReps: { color: G.muted, fontSize: 13, fontWeight: '700' },
  track: { height: 54, borderRadius: 999, backgroundColor: G.panelHi, overflow: 'hidden', position: 'relative', justifyContent: 'center' },
  dashes: { position: 'absolute', left: 0, right: 0, top: 26, height: 2, backgroundColor: G.line },
  fill: { position: 'absolute', left: 0, top: 0, bottom: 0, opacity: 0.22 },
  runner: { position: 'absolute', width: 42, height: 42, borderRadius: 21, backgroundColor: G.greenDim, borderWidth: 3, alignItems: 'center', justifyContent: 'center' },
  runnerIcon: { fontSize: 22 },
  finish: { position: 'absolute', right: 0, top: 0, bottom: 0, width: 14 },
  pct: { color: G.muted, fontSize: 12, fontWeight: '700', textAlign: 'right' },
});

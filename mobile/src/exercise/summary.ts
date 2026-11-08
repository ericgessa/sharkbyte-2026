/**
 * Turns one set into the numbers the results screen shows. Pure: no DOM, no React Native.
 *
 * Everything here DESCRIBES the set relative to itself (how steady the depth was, how even left and right were);
 * nothing is a medical judgement or a "correct form" verdict. Spine values are measured sensor tilt vs the wearer's
 * own calibrated neutral, not validated anatomical angles.
 */

import type { RepMetrics } from '../pose/protocol';
import type { SpineData } from '../pose/wearable';

export type SetStat = { key: string; label: string; value: string; detail: string };

export type SetSummary = {
  reps: number;
  durationMs: number;
  stats: SetStat[];
};

const mean = (xs: number[]) => xs.reduce((a, b) => a + b, 0) / xs.length;
const stdev = (xs: number[]) => {
  const m = mean(xs);
  return Math.sqrt(mean(xs.map((x) => (x - m) ** 2)));
};

export type SetInput = {
  reps: number;
  durationMs: number;
  /** Server-counted reps (push-ups, squats). Empty for jumping jacks. */
  repMetrics: RepMetrics[];
  /** Attempts the server saw but did not count. */
  rejected: number;
  /** Wearable readings received during the set (empty if the spine was off). */
  spine: SpineData[];
};

export function summarizeSet(s: SetInput): SetSummary {
  const stats: SetStat[] = [];
  const minutes = s.durationMs / 60000;

  // Details stay short: each stat is shown inside a small plate.
  if (s.reps > 0 && minutes > 0) {
    stats.push({ key: 'pace', label: 'Pace', value: `${Math.round(s.reps / minutes)}`, detail: 'reps / min' });
  }

  const m = s.repMetrics;
  if (m.length > 0) {
    const tempo = mean(m.map((r) => r.duration_ms)) / 1000;
    stats.push({ key: 'tempo', label: 'Tempo', value: `${tempo.toFixed(1)}s`, detail: 'per rep' });
  }
  if (m.length >= 3) {
    // How much the deepest angle wandered from rep to rep: 0 deg of spread = 100, each degree costs 5.
    const spread = stdev(m.map((r) => r.min_angle_deg));
    const steady = Math.max(0, Math.min(100, Math.round(100 - spread * 5)));
    stats.push({ key: 'depth', label: 'Depth', value: `${steady}`, detail: 'steadiness /100' });
  }
  const asym = m.map((r) => r.max_asymmetry_deg).filter((v): v is number => typeof v === 'number');
  if (asym.length > 0) {
    stats.push({ key: 'balance', label: 'Balance', value: `${mean(asym).toFixed(0)}°`, detail: 'left vs right' });
  }
  if (s.rejected > 0) {
    stats.push({ key: 'rejected', label: 'Not counted', value: `${s.rejected}`, detail: 'too short / fast' });
  }

  const lumbar = s.spine.map((d) => d.spine_lumbar_deg).filter((v): v is number => typeof v === 'number');
  if (lumbar.length > 0) {
    stats.push({ key: 'lumbar', label: 'Lower back', value: `${Math.max(...lumbar).toFixed(0)}°`, detail: `peak bend (avg ${mean(lumbar).toFixed(0)}°)` });
  }
  const lean = s.spine.map((d) => d.imu_pitch_deg).filter((v): v is number => typeof v === 'number');
  if (lean.length > 0) {
    stats.push({ key: 'lean', label: 'Upper body', value: `${Math.max(...lean).toFixed(0)}°`, detail: 'peak lean' });
  }

  return { reps: s.reps, durationMs: s.durationMs, stats };
}

/** "1:05" */
export function formatDuration(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

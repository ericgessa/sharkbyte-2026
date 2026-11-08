/**
 * The rep wave: what the live wave plots each frame, the bar chart after a set (one bar per rep), and the fatigue
 * note. Pure: no DOM, no React Native.
 *
 * Push-ups / squats: depth from the counted joint's angle (180 = straight, smaller = deeper), so every rep is a peak.
 * Jumping jacks: how "open" the body is (arms up + feet apart), so every jack is a peak.
 */
import { LM, type Landmark, type PoseAngles } from '../pose/angles';

const clamp01 = (v: number) => Math.min(1, Math.max(0, v));
const TOP_DEG = 175; // straight
const DEEP_DEG = 70; // about as deep as these exercises go

/** 0 = straight / closed, 1 = deepest / fully open. null when the body isn't visible enough. */
export function waveValue(joint: 'elbow' | 'knee' | null, angles: PoseAngles | null, landmarks: Landmark[] | null): number | null {
  if (joint) {
    if (!angles) return null;
    const pair = joint === 'knee' ? [angles.left_knee_angle_deg, angles.right_knee_angle_deg] : [angles.left_elbow_angle_deg, angles.right_elbow_angle_deg];
    const seen = pair.filter((v): v is number => typeof v === 'number' && Number.isFinite(v));
    if (!seen.length) return null;
    const a = seen.reduce((x, y) => x + y, 0) / seen.length;
    return clamp01((TOP_DEG - a) / (TOP_DEG - DEEP_DEG));
  }
  return jackOpenness(landmarks);
}

/** Jumping jacks: half from the arms (wrists from hip height up to above the head), half from the feet (spread). */
export function jackOpenness(lm: Landmark[] | null): number | null {
  if (!lm) return null;
  const need = [LM.LEFT_SHOULDER, LM.RIGHT_SHOULDER, LM.LEFT_WRIST, LM.RIGHT_WRIST, LM.LEFT_HIP, LM.RIGHT_HIP, LM.LEFT_ANKLE, LM.RIGHT_ANKLE];
  if (need.some((i) => !lm[i] || (lm[i].visibility ?? 1) < 0.5)) return null;
  const hipW = Math.abs(lm[LM.LEFT_HIP].x - lm[LM.RIGHT_HIP].x);
  const torso = Math.abs((lm[LM.LEFT_HIP].y + lm[LM.RIGHT_HIP].y) / 2 - (lm[LM.LEFT_SHOULDER].y + lm[LM.RIGHT_SHOULDER].y) / 2);
  if (hipW < 1e-3 || torso < 1e-3) return null;
  const hipY = (lm[LM.LEFT_HIP].y + lm[LM.RIGHT_HIP].y) / 2;
  const wristY = (lm[LM.LEFT_WRIST].y + lm[LM.RIGHT_WRIST].y) / 2;
  const arms = clamp01((hipY - wristY) / (torso * 1.8)); // wrists at the hips = 0, well above the head = 1
  const spread = Math.abs(lm[LM.LEFT_ANKLE].x - lm[LM.RIGHT_ANKLE].x) / hipW;
  const feet = clamp01((spread - 1) / 2.2); // feet together = 0, wide = 1
  return (arms + feet) / 2;
}

/** One attempt, in order: counted reps and attempts that didn't count. */
export type RepRecord = {
  counted: boolean;
  depthDeg: number | null; // deepest angle (smaller = deeper); null for jumping jacks
  durationMs: number | null;
  backDeg: number | null; // peak lower-back bend during the rep (spine wearable), if worn
};

export type Bar = { h: number; w: number; kind: 'counted' | 'rejected'; cap: number | null };

/** Bars for the chart: height = depth (or a steady height for jumping jacks), width = time, cap = back bend. */
export function repBars(reps: RepRecord[]): Bar[] {
  const durs = reps.map((r) => r.durationMs).filter((v): v is number => typeof v === 'number' && v > 0);
  const maxDur = durs.length ? Math.max(...durs) : 1;
  return reps.map((r) => ({
    h: r.depthDeg === null ? (r.counted ? 0.7 : 0.4) : Math.max(0.12, clamp01((TOP_DEG - r.depthDeg) / (TOP_DEG - DEEP_DEG))),
    w: r.durationMs ? Math.max(0.35, r.durationMs / maxDur) : 0.6,
    kind: r.counted ? 'counted' : 'rejected',
    cap: r.backDeg === null ? null : clamp01(Math.abs(r.backDeg) / 40),
  }));
}

/** A plain-words note when the set visibly faded (6+ counted reps): slower, shallower, or both. */
export function fatigueNote(reps: RepRecord[]): string | null {
  const counted = reps.filter((r) => r.counted);
  if (counted.length < 6) return null;
  const k = Math.floor(counted.length / 3);
  const avg = (xs: (number | null)[]) => {
    const v = xs.filter((x): x is number => typeof x === 'number');
    return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
  };
  const t0 = avg(counted.slice(0, k).map((r) => r.durationMs));
  const t1 = avg(counted.slice(-k).map((r) => r.durationMs));
  const d0 = avg(counted.slice(0, k).map((r) => r.depthDeg));
  const d1 = avg(counted.slice(-k).map((r) => r.depthDeg));
  const slower = t0 && t1 ? Math.round(((t1 - t0) / t0) * 100) : null;
  const shallower = d0 !== null && d1 !== null ? Math.round(d1 - d0) : null;
  if (slower !== null && slower >= 15 && shallower !== null && shallower >= 6) return `Your last reps were ${slower}% slower and ${shallower}° shallower: that's the fade.`;
  if (slower !== null && slower >= 15) return `Your last reps were ${slower}% slower than your first.`;
  if (shallower !== null && shallower >= 6) return `Your last reps were ${shallower}° shallower than your first.`;
  if (slower !== null && slower <= -10) return `You sped up: your last reps were ${-slower}% quicker.`;
  return 'Steady from first rep to last.';
}

/** Server wave bars (d = deepest angle, t = ms) -> chart bars, for the mini waves in Trends. */
export function barsFromServer(bars: { d: number | null; t: number | null }[]): Bar[] {
  return repBars(bars.map((b) => ({ counted: true, depthDeg: b.d, durationMs: b.t, backDeg: null })));
}

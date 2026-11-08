/**
 * "Angle over time" for a finished workout: fetch the stored readings (Tiger Data, through the backend) and turn them
 * into chart-ready series. Pure TypeScript (no React, no DOM): tested in pose.test.mjs.
 *
 * The backend keeps EVERY camera angle and wearable reading of a workout in Tiger Data (a time-series database). This
 * file asks for them downsampled in SQL (`bucket_seconds`) so a chart never pulls the raw stream, then:
 *   - averages the left and right side of the counted joint into one line,
 *   - keeps each wearable back section (lumbar / mid / upper) as its own line,
 *   - turns each saved rep's time window into a band, so you can see the angle dip once per rep.
 */

export type ReadingRow = { time: string; source: string; sensor_id: string; metric: string; value: number; samples: number };
export type ReadingsResponse = { available: boolean; reason?: 'not_configured' | 'unavailable'; readings: ReadingRow[] };

export type Joint = 'knee' | 'elbow';

/** Metric names as stored in Tiger Data (server/app/schemas/events.py ANGLE_FIELDS and sensors.py SENSOR_FIELDS). */
export const JOINT_METRICS: Record<Joint, readonly string[]> = {
  knee: ['left_knee_angle', 'right_knee_angle'],
  elbow: ['left_elbow_angle', 'right_elbow_angle'],
};
export const BACK_METRICS = ['spine_lumbar', 'spine_mid', 'spine_upper'] as const;
export const BACK_LABELS: Record<string, string> = { spine_lumbar: 'Lower back', spine_mid: 'Mid back', spine_upper: 'Upper back' };

export type Point = { t: number; v: number };
export type Series = { key: string; label: string; points: Point[] };
export type RepBand = { rep: number; t0: number; t1: number };

export type ChartData = {
  joint: Joint;
  /** The counted joint (mean of left and right), or null if the camera sent none. */
  angle: Series | null;
  /** Wearable section angles that have data, lower back first. */
  back: Series[];
  repBands: RepBand[];
  /** Seconds from the workout's start to its end (the x axis). */
  durationS: number;
  /** How many readings the chart is built from (before they were averaged into buckets). */
  readings: number;
};

const base = (apiUrl: string) => apiUrl.replace(/\/+$/, '');

export async function getReadings(
  apiUrl: string,
  token: string,
  sessionId: string,
  window: { start: string; end: string; metrics: readonly string[]; bucketSeconds: number; limit?: number },
  fetchImpl: typeof fetch = fetch,
): Promise<ReadingsResponse> {
  const q = new URLSearchParams({ start: window.start, end: window.end, bucket_seconds: String(window.bucketSeconds), limit: String(window.limit ?? 5000) });
  for (const m of window.metrics) q.append('metric', m);
  const res = await fetchImpl(`${base(apiUrl)}/api/workouts/${sessionId}/readings?${q}`, { headers: { Authorization: `Bearer ${token}` } });
  if (!res.ok) {
    let detail = '';
    try {
      const d = ((await res.json()) as { detail?: unknown }).detail;
      if (typeof d === 'string') detail = ` ${d}`;
    } catch {
      // no JSON body
    }
    throw new Error(`${res.status}${detail}`);
  }
  return (await res.json()) as ReadingsResponse;
}

/** The query window for a workout: from its start to its end (a little padding so the last reading is included). */
export function chartWindow(startedAt: string, endedAt: string | null): { start: string; end: string; durationS: number } | null {
  const s = Date.parse(startedAt);
  const e = endedAt ? Date.parse(endedAt) : NaN;
  if (!Number.isFinite(s) || !Number.isFinite(e) || e <= s) return null;
  return { start: new Date(s - 1000).toISOString(), end: new Date(e + 2000).toISOString(), durationS: (e - s) / 1000 };
}

/** About `targetPoints` points per line however long the workout is, never finer than 0.1 s. */
export function bucketSecondsFor(durationS: number, targetPoints = 240): number {
  return Math.max(0.1, Math.round((durationS / targetPoints) * 100) / 100);
}

export function metricsToFetch(joint: Joint): string[] {
  return [...JOINT_METRICS[joint], ...BACK_METRICS];
}

type RepLike = { rep_number: number; metrics: { started_at?: string; ended_at?: string } };

/** Seconds on the chart's x axis (0 = the workout's start). Clamped into the workout so a stray reading cannot widen the axis. */
const secondsSince = (iso: string, startMs: number) => (Date.parse(iso) - startMs) / 1000;

export function buildChart(rows: readonly ReadingRow[], startedAt: string, endedAt: string | null, joint: Joint, reps: readonly RepLike[] = []): ChartData {
  const startMs = Date.parse(startedAt);
  const win = chartWindow(startedAt, endedAt);
  const durationS = win?.durationS ?? Math.max(0, ...rows.map((r) => secondsSince(r.time, startMs)));

  const jointSet = new Set(JOINT_METRICS[joint]);
  const byTime = new Map<string, { sum: number; n: number }>();
  const back = new Map<string, Point[]>();
  let readings = 0;
  for (const r of rows) {
    if (!Number.isFinite(r.value)) continue;
    const t = secondsSince(r.time, startMs);
    if (!Number.isFinite(t)) continue;
    if (jointSet.has(r.metric)) {
      const cur = byTime.get(r.time) ?? { sum: 0, n: 0 };
      cur.sum += r.value;
      cur.n += 1;
      byTime.set(r.time, cur);
      readings += r.samples ?? 1;
    } else if ((BACK_METRICS as readonly string[]).includes(r.metric)) {
      const list = back.get(r.metric) ?? [];
      list.push({ t, v: r.value });
      back.set(r.metric, list);
      readings += r.samples ?? 1;
    }
  }

  const anglePoints = [...byTime.entries()].map(([time, a]) => ({ t: secondsSince(time, startMs), v: a.sum / a.n })).sort((a, b) => a.t - b.t);
  const joinLabel = joint === 'knee' ? 'Knee angle' : 'Elbow angle';

  const repBands: RepBand[] = [];
  for (const rep of reps) {
    const a = rep.metrics.started_at ? secondsSince(rep.metrics.started_at, startMs) : NaN;
    const b = rep.metrics.ended_at ? secondsSince(rep.metrics.ended_at, startMs) : NaN;
    if (Number.isFinite(a) && Number.isFinite(b) && b > a) repBands.push({ rep: rep.rep_number, t0: Math.max(0, a), t1: durationS > 0 ? Math.min(b, durationS) : b });
  }

  return {
    joint,
    angle: anglePoints.length ? { key: 'angle', label: joinLabel, points: anglePoints } : null,
    back: BACK_METRICS.filter((m) => back.has(m)).map((m) => ({ key: m, label: BACK_LABELS[m], points: back.get(m)!.sort((a, b) => a.t - b.t) })),
    repBands,
    durationS,
    readings,
  };
}

// ------------------------------------------------------------------ drawing helpers (pure geometry)

/** A tidy axis range around the data: padded, and rounded out to a multiple of `step`. */
export function niceDomain(values: readonly number[], step = 10, fallback: [number, number] = [0, 180]): [number, number] {
  const finite = values.filter(Number.isFinite);
  if (!finite.length) return fallback;
  const lo = Math.floor((Math.min(...finite) - 2) / step) * step;
  const hi = Math.ceil((Math.max(...finite) + 2) / step) * step;
  return hi - lo < step * 2 ? [lo, lo + step * 2] : [lo, hi];
}

export function ticks(domain: readonly [number, number], count = 4): number[] {
  const [lo, hi] = domain;
  const step = (hi - lo) / count;
  return Array.from({ length: count + 1 }, (_, i) => Math.round((lo + step * i) * 10) / 10);
}

/** SVG path data for a line, in a plot area of `w` x `h` (y grows downward, so larger values are higher). A gap in the data (longer than `gapS`) breaks the line instead of drawing a false slope across it. */
export function linePath(points: readonly Point[], x: readonly [number, number], y: readonly [number, number], w: number, h: number, gapS = 1.5): string {
  const sx = (t: number) => ((t - x[0]) / (x[1] - x[0] || 1)) * w;
  const sy = (v: number) => h - ((v - y[0]) / (y[1] - y[0] || 1)) * h;
  let d = '';
  let prev: Point | null = null;
  for (const p of points) {
    if (p.t < x[0] || p.t > x[1]) continue;
    const cmd = prev === null || p.t - prev.t > gapS ? 'M' : 'L';
    d += `${cmd}${sx(p.t).toFixed(1)} ${sy(p.v).toFixed(1)} `;
    prev = p;
  }
  return d.trim();
}

export function formatSeconds(s: number): string {
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.round(s % 60)).padStart(2, '0')}`;
}

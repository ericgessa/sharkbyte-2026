/**
 * Workout history: the list, one workout's details, and delete (docs/frontend-integration.md section 3), plus the pure
 * helpers that turn it into display text. No React, no DOM: tested in pose.test.mjs.
 */

export type WorkoutListItem = {
  session_id: string;
  exercise: string;
  analysis_supported: boolean;
  status: 'active' | 'completed';
  started_at: string;
  ended_at: string | null;
  total_reps: number;
};

export type WorkoutDetail = {
  workout: WorkoutListItem;
  stats: {
    rep_count: number;
    avg_min_angle_deg: number | null;
    deepest_angle_deg: number | null;
    shallowest_angle_deg: number | null;
    depth_change_deg: number | null;
    max_asymmetry_deg?: number | null;
    avg_rep_duration_ms?: number | null;
  };
  reps: { rep_number: number; metrics: { min_angle_deg?: number; duration_ms?: number; joint?: string }; created_at: string }[];
  coaching_events: { rep_number: number | null; issue_code: string | null; message: string; action: string | null; created_at: string }[];
};

const base = (apiUrl: string) => apiUrl.replace(/\/+$/, '');
const auth = (token: string) => ({ Authorization: `Bearer ${token}` });

async function failure(res: Response): Promise<Error> {
  let detail = '';
  try {
    const d = ((await res.json()) as { detail?: unknown }).detail;
    if (typeof d === 'string') detail = ` ${d}`;
  } catch {
    // no JSON body
  }
  return new Error(`${res.status}${detail}`);
}

/** Newest first. The API returns at most 100. */
export async function listWorkouts(apiUrl: string, token: string, limit = 100, fetchImpl: typeof fetch = fetch): Promise<WorkoutListItem[]> {
  const res = await fetchImpl(`${base(apiUrl)}/api/workouts?limit=${limit}`, { headers: auth(token) });
  if (!res.ok) throw await failure(res);
  return (await res.json()) as WorkoutListItem[];
}

export async function getWorkoutDetail(apiUrl: string, token: string, id: string, fetchImpl: typeof fetch = fetch): Promise<WorkoutDetail> {
  const res = await fetchImpl(`${base(apiUrl)}/api/workouts/${id}/summary`, { headers: auth(token) });
  if (!res.ok) throw await failure(res);
  return (await res.json()) as WorkoutDetail;
}

/** Permanent: removes the reps, coaching notes and sensor data too. A 503 means nothing was deleted; try again. */
export async function deleteWorkout(apiUrl: string, token: string, id: string, fetchImpl: typeof fetch = fetch): Promise<void> {
  const res = await fetchImpl(`${base(apiUrl)}/api/workouts/${id}`, { method: 'DELETE', headers: auth(token) });
  if (!res.ok) throw await failure(res);
}

/** "squat" -> "Squat", "push-up" -> "Push-up" */
export function exerciseTitle(exercise: string): string {
  const name = exercise.replace(/_/g, ' ');
  return name.charAt(0).toUpperCase() + name.slice(1);
}

/** "Today", "Yesterday", or "Mon, Nov 3" (with the year if it is not this year). In the user's own time zone. */
export function dayLabel(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((startOf(now) - startOf(d)) / 86_400_000);
  if (days === 0) return 'Today';
  if (days === 1) return 'Yesterday';
  const opts: Intl.DateTimeFormatOptions = { weekday: 'short', month: 'short', day: 'numeric' };
  if (d.getFullYear() !== now.getFullYear()) opts.year = 'numeric';
  return d.toLocaleDateString('en-US', opts);
}

/** "6:05 PM" */
export function timeLabel(iso: string): string {
  return new Date(iso).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
}

/** Wall-clock length: "45 s", "12 min", "1 h 5 min". null while the workout is still open. */
export function workoutLength(startedAt: string, endedAt: string | null): string | null {
  if (!endedAt) return null;
  const s = Math.max(0, Math.round((new Date(endedAt).getTime() - new Date(startedAt).getTime()) / 1000));
  if (s < 60) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  return m % 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m / 60} h`;
}

/** The list grouped by day, newest day first, keeping the API's newest-first order inside each day. */
export function groupByDay<T extends { started_at: string }>(items: T[], now: Date = new Date()): { day: string; items: T[] }[] {
  const groups: { day: string; items: T[] }[] = [];
  for (const item of items) {
    const day = dayLabel(item.started_at, now);
    const last = groups[groups.length - 1];
    if (last && last.day === day) last.items.push(item);
    else groups.push({ day, items: [item] });
  }
  return groups;
}

/** How a coaching note is labelled in the timeline. */
export function noteKind(action: string | null): 'not counted' | 'rest' | 'cue' {
  if (action === 'rep_rejected') return 'not counted';
  if (action === 'recommend_rest') return 'rest';
  return 'cue';
}

/**
 * The user's progress across all workouts: GET /api/stats (docs/event-contracts.md), plus the pure helpers that turn
 * it into display text. No React, no DOM: tested in pose.test.mjs.
 */

export type WeekActivity = { week_start: string; workouts: number; reps: number };

export type Progress = {
  totals: { workouts: number; reps: number; active_seconds: number; first_workout_at: string | null; last_workout_at: string | null };
  per_exercise: {
    exercise: string;
    workouts: number;
    total_reps: number;
    best_workout_reps: number;
    best_workout_id: string | null;
    last_workout_at: string | null;
  }[];
  /** Oldest first, ending with the current week. Empty weeks are zeros. */
  weekly: WeekActivity[];
  streak: { current_days: number; longest_days: number };
  records: { most_reps_in_a_workout: { exercise: string; reps: number; workout_id: string | null; at: string | null } | null };
  /** Rep Racer. null until the GRIT Games tables are set up. */
  games: {
    races: number;
    finished: number;
    fastest: { exercise: string; target_reps: number; duration_ms: number; mode: string; achieved_at: string | null }[];
  } | null;
  workouts_considered: number;
};

/** Minutes east of UTC, as the API wants it (JS reports the opposite sign). */
export function localTzOffsetMinutes(now: Date = new Date()): number {
  return -now.getTimezoneOffset();
}

export async function getProgress(
  apiUrl: string,
  accessToken: string,
  { weeks = 8, tzOffsetMinutes = localTzOffsetMinutes() }: { weeks?: number; tzOffsetMinutes?: number } = {},
  fetchImpl: typeof fetch = fetch,
): Promise<Progress> {
  const url = `${apiUrl.replace(/\/+$/, '')}/api/stats?tz_offset_minutes=${tzOffsetMinutes}&weeks=${weeks}`;
  const res = await fetchImpl(url, { headers: { Authorization: `Bearer ${accessToken}` } });
  if (!res.ok) throw new Error(`${res.status}`);
  return (await res.json()) as Progress;
}

/** 1,284 / 12.9K / 1.2M */
export function compactNumber(n: number): string {
  if (n < 10_000) return n.toLocaleString('en-US');
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 100_000 ? 1 : 0).replace(/\.0$/, '')}K`;
  return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`;
}

/** 0 -> "0m", 2700 -> "45m", 12000 -> "3h 20m" */
export function formatDuration(seconds: number): string {
  const m = Math.round(seconds / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
}

/** "squat" -> "squats", "push-up" -> "push-ups"; 1 stays singular. */
export function exerciseNoun(exercise: string, count: number): string {
  const name = exercise.replace(/_/g, ' ');
  return count === 1 || name.endsWith('s') ? name : `${name}s`;
}

/** Same calendar day in the user's own time zone (the API's days are the user's days too). */
export function isSameLocalDay(iso: string | null, now: Date = new Date()): boolean {
  if (!iso) return false;
  const d = new Date(iso);
  return d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
}

/** The line under the streak number: what to do today. */
export function streakHint(p: Pick<Progress, 'streak' | 'totals'>, now: Date = new Date()): string {
  const { current_days: current, longest_days: longest } = p.streak;
  if (p.totals.workouts === 0) return 'Finish your first workout to start a streak.';
  if (isSameLocalDay(p.totals.last_workout_at, now)) {
    return current > 1 && current >= longest ? 'Done for today. This is your best streak yet.' : 'Done for today. See you tomorrow.';
  }
  if (current > 0) return 'Train today to keep it going.';
  return longest > 0 ? `Your best was ${longest} ${longest === 1 ? 'day' : 'days'}. Start a new one today.` : 'Train today to start a streak.';
}

/** "2026-11-02" -> "Nov 2". Parsed as a plain date (no time zone shift). */
export function shortWeekLabel(weekStart: string): string {
  const [y, m, d] = weekStart.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

/** The fastest Rep Racer finish, e.g. "10 squats in 24.3 s", or null if there is none. */
export function fastestRace(games: Progress['games']): string | null {
  const best = games?.fastest?.slice().sort((a, b) => a.duration_ms - b.duration_ms)[0];
  if (!best) return null;
  return `${best.target_reps} ${exerciseNoun(best.exercise, best.target_reps)} in ${(best.duration_ms / 1000).toFixed(1)} s`;
}

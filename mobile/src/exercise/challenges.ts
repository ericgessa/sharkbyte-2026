/**
 * Challenges: beat your best set + 1 within 3:00. Pure: no DOM, no React Native.
 *
 * A finished challenge is saved as a GRIT Games Rep Racer "solo" result with a 3:00 limit (POST /api/games/results),
 * so the SERVER scores it from the workout's own counted reps: a win can't be claimed from the phone. The games tables
 * only take squats and push-ups, so jumping-jack challenges are played but not recorded (yet).
 */
import type { GameResult } from '../games/games-api';
import type { ExerciseStats } from '../pose/workout-client';

export const CHALLENGE_TIME_S = 180;
export const MAX_SAVED_TARGET = 100; // the games tables' limit on target_reps
const SAVED_EXERCISES = new Set(['push-up', 'squat']);

export type ChallengeState = {
  locked: boolean; // no counted set yet: there's nothing to beat
  best: number;
  target: number;
  recorded: boolean; // will a finished try be saved to the record?
};

export function challengeFor(exercise: string, stats: ExerciseStats | undefined): ChallengeState {
  const best = stats && stats.workouts > 0 ? stats.best_workout_reps : 0;
  const target = best + 1;
  return { locked: best < 1, best, target, recorded: SAVED_EXERCISES.has(exercise) && target <= MAX_SAVED_TARGET };
}

/** Saved results that were challenges (solo Rep Racer rounds with the 3:00 limit). */
export function isChallengeResult(r: GameResult): boolean {
  return r.game === 'rep_racer' && r.mode === 'solo' && r.time_limit_s === CHALLENGE_TIME_S;
}

export type ChallengeRecord = { tries: number; wins: number; streak: number; last: GameResult | null };

/** One exercise's challenge record, from saved results (any order). `streak` = wins in a row up to the latest try. */
export function challengeRecord(results: GameResult[], exercise: string): ChallengeRecord {
  const mine = results.filter((r) => isChallengeResult(r) && r.exercise === exercise).sort((a, b) => b.created_at.localeCompare(a.created_at));
  let streak = 0;
  for (const r of mine) {
    if (!r.completed) break;
    streak += 1;
  }
  return { tries: mine.length, wins: mine.filter((r) => r.completed).length, streak, last: mine[0] ?? null };
}

/** The body for POST /api/games/results, or null when this try can't be recorded. */
export function challengeResultBody(exercise: string, workoutSessionId: string, target: number, durationMs: number) {
  if (!SAVED_EXERCISES.has(exercise) || target < 1 || target > MAX_SAVED_TARGET) return null;
  return {
    game: 'rep_racer' as const,
    mode: 'solo' as const,
    exercise: exercise as 'push-up' | 'squat',
    workout_session_id: workoutSessionId,
    duration_ms: Math.max(0, Math.min(Math.round(durationMs), CHALLENGE_TIME_S * 1000)),
    target_reps: target,
    time_limit_s: CHALLENGE_TIME_S,
  };
}

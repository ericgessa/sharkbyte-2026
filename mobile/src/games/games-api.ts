/**
 * REST calls for GRIT Games results (see docs/grit-games.md). Same conventions as pose/workout-client.ts: a plain
 * `fetch` with the signed-in user's access token, injectable for tests.
 */

import type { ExerciseSlug } from './exercise';

export type GameId = 'rep_racer';
export type GameMode = 'solo' | 'personal_best' | 'multiplayer';

export type PersonalBestComparison = {
  has_previous: boolean;
  previous_duration_ms: number | null;
  previous_reps: number | null;
  is_new_best: boolean;
  delta_ms: number | null;
  reps_delta: number | null;
};

export type GameResult = {
  id: string;
  game: GameId;
  mode: GameMode;
  exercise: ExerciseSlug;
  valid_reps: number;
  score: number;
  duration_ms: number;
  target_reps: number | null;
  time_limit_s: number | null;
  completed: boolean;
  form_feedback: Record<string, number>;
  rep_times_ms: number[];
  workout_session_id: string | null;
  room_id: string | null;
  created_at: string;
  personal_best?: PersonalBestComparison | null;
};

export type SaveResultBody = {
  game: GameId;
  mode: GameMode;
  exercise: ExerciseSlug;
  workout_session_id: string;
  score?: number;
  duration_ms: number;
  target_reps?: number;
  time_limit_s?: number;
  form_feedback?: Record<string, number>;
};

export class GamesApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
  /** The migration for the games tables has not been applied to this Supabase project. */
  get tablesMissing(): boolean {
    return this.status === 503 && /tables are missing/i.test(this.message);
  }
}

async function failure(res: Response): Promise<GamesApiError> {
  let detail = '';
  try {
    const d = ((await res.json()) as { detail?: unknown }).detail;
    detail = typeof d === 'string' ? d : typeof d === 'object' && d && 'detail' in d ? String((d as { detail: unknown }).detail) : Array.isArray(d) ? String((d[0] as { msg?: unknown })?.msg ?? '') : '';
  } catch {
    // no JSON body
  }
  return new GamesApiError(res.status, detail || `request failed (${res.status})`);
}

function headers(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' };
}

const base = (apiUrl: string) => apiUrl.replace(/\/+$/, '');

export async function saveGameResult(apiUrl: string, token: string, body: SaveResultBody, fetchImpl: typeof fetch = fetch): Promise<GameResult> {
  const res = await fetchImpl(`${base(apiUrl)}/api/games/results`, { method: 'POST', headers: headers(token), body: JSON.stringify(body) });
  if (!res.ok) throw await failure(res);
  return (await res.json()) as GameResult;
}

export async function getBest(
  apiUrl: string,
  token: string,
  game: GameId,
  exercise: ExerciseSlug,
  targetReps?: number,
  fetchImpl: typeof fetch = fetch,
): Promise<{ best: GameResult | null; attempts: number }> {
  const q = new URLSearchParams({ game, exercise });
  if (targetReps !== undefined) q.set('target_reps', String(targetReps));
  const res = await fetchImpl(`${base(apiUrl)}/api/games/best?${q}`, { headers: headers(token) });
  if (!res.ok) throw await failure(res);
  return (await res.json()) as { best: GameResult | null; attempts: number };
}

export async function listGameResults(apiUrl: string, token: string, game?: GameId, limit = 20, fetchImpl: typeof fetch = fetch): Promise<GameResult[]> {
  const q = new URLSearchParams({ limit: String(limit) });
  if (game) q.set('game', game);
  const res = await fetchImpl(`${base(apiUrl)}/api/games/results?${q}`, { headers: headers(token) });
  if (!res.ok) throw await failure(res);
  return (await res.json()) as GameResult[];
}

/** The short AI recap of the round's workout. `source` is "agent" when Gemini/Claude wrote it, else "rules". */
export async function getCoachSummary(apiUrl: string, token: string, workoutSessionId: string, fetchImpl: typeof fetch = fetch): Promise<{ summary: string; source: 'agent' | 'rules' }> {
  const res = await fetchImpl(`${base(apiUrl)}/api/workouts/${workoutSessionId}/coach-summary`, { headers: headers(token) });
  if (!res.ok) throw await failure(res);
  return (await res.json()) as { summary: string; source: 'agent' | 'rules' };
}

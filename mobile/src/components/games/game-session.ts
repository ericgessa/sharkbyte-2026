import { useRef, useState } from 'react';

import { ExerciseController, type ControllerHandlers } from '@/games/exercise-controller';
import type { ExerciseSlug } from '@/games/exercise';
import { getCoachSummary, saveGameResult, type GameResult, type SaveResultBody, GamesApiError } from '@/games/games-api';
import { getApiUrl } from '@/utils/api-url';
import { getAccessToken } from '@/utils/auth';

import { useSession } from '@/components/session-provider';

export function makeController(exercise: ExerciseSlug, handlers: ControllerHandlers): ExerciseController {
  return new ExerciseController({ exercise, apiUrl: getApiUrl(), getToken: getAccessToken, handlers });
}

export type SavedRound = {
  workoutSessionId: string | null;
  /** The server's own count of reps for this round. */
  serverReps: number | null;
  result: GameResult | null;
  /** Why the result was not stored, in words a player can read. */
  problem: string | null;
};

/**
 * Ends a camera round: finishes the workout (so every counted rep is saved) and stores the game result. The server
 * scores the result from its own record of the workout, so what comes back is the authoritative version. Never throws:
 * a failure is returned as `problem` and the results screen still shows the round.
 */
export async function finishAndSave(controller: ExerciseController, body: Omit<SaveResultBody, 'workout_session_id'>): Promise<SavedRound> {
  let sessionId: string | null = controller.sessionId;
  let serverReps: number | null = null;
  try {
    const done = await controller.finish();
    sessionId = done.sessionId;
    serverReps = done.totalReps;
  } catch (e) {
    return { workoutSessionId: sessionId, serverReps, result: null, problem: `The workout could not be saved: ${e instanceof Error ? e.message : String(e)}` };
  }
  try {
    const result = await saveGameResult(getApiUrl(), await getAccessToken(), { ...body, workout_session_id: sessionId });
    return { workoutSessionId: sessionId, serverReps, result, problem: null };
  } catch (e) {
    const missing = e instanceof GamesApiError && e.tablesMissing;
    return {
      workoutSessionId: sessionId,
      serverReps,
      result: null,
      problem: missing
        ? 'Your reps were saved as a workout, but the GRIT Games tables are not set up in Supabase yet (apply supabase/migrations/20261108000001_grit_games.sql), so this result is not in your game history.'
        : `The result could not be saved: ${e instanceof Error ? e.message : String(e)}`,
    };
  }
}

/** The short AI recap of a finished round. `source` says whether Gemini/Claude or the built-in rules wrote it. */
export function useCoachSummary(workoutSessionId: string | null) {
  const [state, setState] = useState<{ status: 'idle' | 'loading' | 'done' | 'error'; text: string; source: 'agent' | 'rules' | null }>({ status: 'idle', text: '', source: null });
  const asked = useRef<string | null>(null);

  const load = async () => {
    if (!workoutSessionId || asked.current === workoutSessionId) return;
    asked.current = workoutSessionId;
    setState({ status: 'loading', text: '', source: null });
    try {
      const r = await getCoachSummary(getApiUrl(), await getAccessToken(), workoutSessionId);
      setState({ status: 'done', text: r.summary, source: r.source });
    } catch (e) {
      asked.current = null;
      setState({ status: 'error', text: e instanceof Error ? e.message : String(e), source: null });
    }
  };
  return { ...state, load };
}

/** What to call the player in a race: their display name, else the part of their email before the @. */
export function useDisplayName(): string {
  const { session } = useSession();
  const meta = session?.user.user_metadata as { display_name?: string; full_name?: string } | undefined;
  return (meta?.display_name || meta?.full_name || session?.user.email?.split('@')[0] || 'Player').slice(0, 40);
}

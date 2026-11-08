/**
 * Rep Racer's rules as pure functions/classes: no timers, no canvas, no network. The caller supplies the elapsed
 * time, so every rule (goal reached, time expired, progress, ghost replay) is deterministic and tested.
 */

export const TARGET_PRESETS = [5, 10, 15, 20] as const;
export const TIME_PRESETS_S = [30, 60, 90, 120] as const;
export const MIN_TARGET = 1;
export const MAX_TARGET = 100;
export const MIN_TIME_S = 10;
export const MAX_TIME_S = 600;

/** A custom value typed by the player -> a whole number inside the range the server accepts (or null if it is not a number). */
export function parseSetting(text: string, min: number, max: number): number | null {
  const n = Number.parseInt(text.trim(), 10);
  if (!Number.isFinite(n) || !/^\s*\d+\s*$/.test(text)) return null;
  return Math.min(max, Math.max(min, n));
}

export type RaceStatus = 'idle' | 'running' | 'won' | 'expired';

export class SoloRace {
  status: RaceStatus = 'idle';
  reps = 0;
  /** Milliseconds from the start to each counted rep. */
  repTimesMs: number[] = [];
  elapsedMs = 0;
  /** When the target was reached (only when won). */
  finishMs: number | null = null;

  readonly targetReps: number;
  readonly timeLimitMs: number;

  constructor(targetReps: number, timeLimitMs: number) {
    this.targetReps = targetReps;
    this.timeLimitMs = timeLimitMs;
  }

  start(): void {
    this.status = 'running';
    this.reps = 0;
    this.repTimesMs = [];
    this.elapsedMs = 0;
    this.finishMs = null;
  }

  /** Move the clock forward. Ends the race when the time limit is reached. */
  tick(elapsedMs: number): void {
    if (this.status !== 'running') return;
    this.elapsedMs = Math.min(Math.max(elapsedMs, this.elapsedMs), this.timeLimitMs);
    if (this.elapsedMs >= this.timeLimitMs) this.status = 'expired';
  }

  /** One server-counted rep. Returns false (and changes nothing) if the race is not running, e.g. after the buzzer. */
  onRep(elapsedMs: number): boolean {
    if (this.status !== 'running') return false;
    this.tick(elapsedMs);
    if (this.status !== 'running') return false; // the buzzer went first
    this.reps += 1;
    this.repTimesMs.push(this.elapsedMs);
    if (this.reps >= this.targetReps) {
      this.status = 'won';
      this.finishMs = this.elapsedMs;
    }
    return true;
  }

  get progress(): number {
    return Math.min(1, this.reps / this.targetReps);
  }
  get remainingMs(): number {
    return Math.max(0, this.timeLimitMs - this.elapsedMs);
  }
  get finished(): boolean {
    return this.status === 'won' || this.status === 'expired';
  }
}

export type GhostFrame = { reps: number; /** 0..1 along the track, smooth between reps */ progress: number; finished: boolean };

/**
 * Where a previous attempt was at `elapsedMs`, replayed from its recorded rep timestamps. Nothing is invented: the
 * ghost moves only because a real rep was recorded at that time, and it stops at the last recorded rep. Between two
 * reps it glides, so the runner looks alive rather than jumping.
 */
export function ghostAt(repTimesMs: readonly number[], elapsedMs: number, targetReps: number): GhostFrame {
  if (targetReps <= 0) return { reps: 0, progress: 0, finished: true };
  const times = repTimesMs.slice(0, targetReps);
  let reps = 0;
  while (reps < times.length && times[reps] <= elapsedMs) reps += 1;
  const finished = reps >= times.length && times.length > 0 && elapsedMs >= times[times.length - 1];
  let whole = reps;
  if (reps < times.length) {
    const from = reps === 0 ? 0 : times[reps - 1];
    const span = times[reps] - from;
    whole = reps + (span > 0 ? Math.min(1, Math.max(0, (elapsedMs - from) / span)) : 0);
  }
  return { reps, progress: Math.min(1, whole / targetReps), finished };
}

export type Comparison = { kind: 'first' | 'faster' | 'slower' | 'same' | 'more_reps' | 'fewer_reps' | 'same_reps'; text: string };

export function formatDuration(ms: number): string {
  const s = Math.max(0, ms) / 1000;
  return `${s.toFixed(1)} s`;
}

/** The sentence under the result: improvement or decline against the previous best, from saved numbers only. */
export function describeComparison(
  now: { completed: boolean; durationMs: number; reps: number },
  previous: { completed: boolean; durationMs: number; reps: number } | null,
): Comparison {
  if (!previous) return { kind: 'first', text: now.completed ? 'First recorded race: this is your time to beat.' : 'First recorded race. Reach the target to set a time to beat.' };
  if (now.completed && previous.completed) {
    const delta = now.durationMs - previous.durationMs;
    if (delta === 0) return { kind: 'same', text: `Exactly your best time, ${formatDuration(now.durationMs)}.` };
    return delta < 0
      ? { kind: 'faster', text: `New personal best: ${formatDuration(-delta)} faster than before (${formatDuration(previous.durationMs)}).` }
      : { kind: 'slower', text: `${formatDuration(delta)} slower than your best (${formatDuration(previous.durationMs)}).` };
  }
  if (now.completed) return { kind: 'faster', text: `You finished a race your previous attempt did not (it stopped at ${previous.reps} reps).` };
  const diff = now.reps - previous.reps;
  if (diff > 0) return { kind: 'more_reps', text: `${diff} more rep${diff === 1 ? '' : 's'} than your previous attempt.` };
  if (diff < 0) return { kind: 'fewer_reps', text: `${-diff} fewer rep${diff === -1 ? '' : 's'} than your previous attempt.` };
  return { kind: 'same_reps', text: `Same number of reps as your previous attempt (${previous.reps}).` };
}

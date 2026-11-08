/**
 * Telling two people in one camera view apart, as pure functions.
 *
 * MediaPipe returns the people it found in no fixed order and can swap them between frames, so the order of its list
 * cannot identify a player. Position does: Player 1 is whoever is on the LEFT of the screen as the players see it
 * (the preview is a mirror), Player 2 the right. The game asks the two to stand side by side.
 */

import type { Landmark } from '../pose/angles';
import { LM } from '../pose/angles';

export type Slot = 0 | 1;

/**
 * Where the person is across the screen as the players see it (0 = left edge, 1 = right edge). Uses the hips (the
 * middle of the body), else the shoulders; null if neither is visible. The camera image is mirrored on screen, so
 * the raw image x is flipped here.
 */
export function screenCenterX(landmarks: readonly Landmark[], minVisibility = 0.4): number | null {
  const pick = (a: number, b: number): number | null => {
    const seen = [landmarks[a], landmarks[b]].filter((p): p is Landmark => !!p && (p.visibility ?? 0) >= minVisibility);
    return seen.length ? seen.reduce((sum, p) => sum + p.x, 0) / seen.length : null;
  };
  const x = pick(LM.LEFT_HIP, LM.RIGHT_HIP) ?? pick(LM.LEFT_SHOULDER, LM.RIGHT_SHOULDER);
  return x === null ? null : 1 - x;
}

/**
 * Which detected person is Player 1 and which is Player 2. `xs` is each person's `screenCenterX` (null = unusable).
 * Returns, for slot 0 and slot 1, the index into `xs` of the person playing it, or null if that side is empty.
 *  - Two usable people: the leftmost is Player 1, the other Player 2, however far apart they stand.
 *  - One usable person: they play the side of the screen they are standing on (left half / right half).
 */
export function assignPlayers(xs: readonly (number | null)[]): [number | null, number | null] {
  const people = xs.map((x, index) => ({ x, index })).filter((p): p is { x: number; index: number } => p.x !== null);
  if (people.length === 0) return [null, null];
  if (people.length === 1) return people[0].x < 0.5 ? [people[0].index, null] : [null, people[0].index];
  const [left, right] = [...people].sort((a, b) => a.x - b.x);
  return [left.index, right.index];
}

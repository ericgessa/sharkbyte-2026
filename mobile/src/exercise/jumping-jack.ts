/**
 * Counts jumping jacks on the phone from MediaPipe image landmarks. Pure: no DOM, no React Native.
 *
 * Open  = both wrists above the shoulders AND ankles spread wider than OPEN_SPREAD x hip width.
 * Closed = both wrists below the shoulders AND ankles closer than CLOSED_SPREAD x hip width.
 * One rep = closed -> open -> closed. The gap between the two thresholds (hysteresis) stops jitter from double
 * counting, and a minimum rep time ignores impossible speeds. Frames where the joints aren't visible are skipped.
 * Image y grows downwards, so "above" means a smaller y.
 */

import { LM, type Landmark } from '../pose/angles';

const MIN_VISIBILITY = 0.5;
const OPEN_SPREAD = 1.6;
const CLOSED_SPREAD = 1.25;
const MIN_REP_MS = 400;

type Pose = 'open' | 'closed' | 'between';

export function classifyJack(lm: Landmark[] | null): Pose | null {
  if (!lm) return null;
  const need = [LM.LEFT_SHOULDER, LM.RIGHT_SHOULDER, LM.LEFT_WRIST, LM.RIGHT_WRIST, LM.LEFT_HIP, LM.RIGHT_HIP, LM.LEFT_ANKLE, LM.RIGHT_ANKLE];
  for (const i of need) {
    const p = lm[i];
    if (!p || (p.visibility ?? 1) < MIN_VISIBILITY) return null;
  }
  const hipWidth = Math.abs(lm[LM.LEFT_HIP].x - lm[LM.RIGHT_HIP].x);
  if (hipWidth < 1e-3) return null;
  const spread = Math.abs(lm[LM.LEFT_ANKLE].x - lm[LM.RIGHT_ANKLE].x) / hipWidth;
  const shoulderY = (lm[LM.LEFT_SHOULDER].y + lm[LM.RIGHT_SHOULDER].y) / 2;
  const armsUp = lm[LM.LEFT_WRIST].y < shoulderY && lm[LM.RIGHT_WRIST].y < shoulderY;
  const armsDown = lm[LM.LEFT_WRIST].y > shoulderY && lm[LM.RIGHT_WRIST].y > shoulderY;
  if (armsUp && spread >= OPEN_SPREAD) return 'open';
  if (armsDown && spread <= CLOSED_SPREAD) return 'closed';
  return 'between';
}

export class JumpingJackCounter {
  count = 0;
  private wasOpen = false;
  private seenClosed = false;
  private lastRepAt = Number.NEGATIVE_INFINITY;

  /** Feed one frame; returns true when this frame completed a rep. `nowMs` is any increasing clock. */
  update(lm: Landmark[] | null, nowMs: number): boolean {
    const pose = classifyJack(lm);
    if (pose === 'closed') {
      const completed = this.wasOpen && this.seenClosed && nowMs - this.lastRepAt >= MIN_REP_MS;
      this.seenClosed = true;
      this.wasOpen = false;
      if (completed) {
        this.count += 1;
        this.lastRepAt = nowMs;
        return true;
      }
    } else if (pose === 'open' && this.seenClosed) {
      this.wasOpen = true;
    }
    return false;
  }
}

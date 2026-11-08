/**
 * What GRIT Games needs to know about an exercise, as pure functions (no DOM, no React Native, no network).
 *
 * Rep COUNTING is not done here: the server's rep counter (the same one workouts use) counts reps from the joint
 * angles we stream. This file turns the same angles into two things the games need instantly, without a round trip:
 *   - `bodyPosition`: where the body is in the movement, 0 (top: standing / arms extended) to 1 (bottom).
 *   - `FormMonitor`: descriptive, rate-limited hints ("Stand fully before the next rep."). They never count anything.
 */

import type { PoseAngles } from '../pose/angles';

export type ExerciseSlug = 'squat' | 'push-up';
export const EXERCISES: readonly ExerciseSlug[] = ['squat', 'push-up'];

/** What a game receives for every rep the SERVER counted. One counted rep is exactly one event. */
export type ExerciseRepEvent = {
  exercise: ExerciseSlug;
  /** Epoch ms on this device when the rep arrived. */
  timestamp: number;
  /** 1, 2, 3 ... as numbered by the server; strictly increasing. */
  repNumber: number;
  /** The server only reports reps that completed a full movement cycle, so this is true for every delivered rep. */
  isValid: boolean;
  formFeedback?: string;
};

export type ExerciseProfile = {
  slug: ExerciseSlug;
  label: string;
  joint: 'knee' | 'elbow';
  /** Joint angle (degrees) with the body at the top of the movement and at the bottom. */
  topDeg: number;
  bottomDeg: number;
  /** The top is never assumed lower than this, however little of the range the person shows. */
  topFloorDeg: number;
  /** A counted rep whose deepest angle is at or below this is a "good" rep; above it, we ask for more depth. */
  goodDepthDeg: number;
  deeperMessage: string;
};

export const PROFILES: Record<ExerciseSlug, ExerciseProfile> = {
  squat: { slug: 'squat', label: 'Squats', joint: 'knee', topDeg: 165, bottomDeg: 100, topFloorDeg: 145, goodDepthDeg: 105, deeperMessage: 'Squat deeper.' },
  'push-up': { slug: 'push-up', label: 'Push-ups', joint: 'elbow', topDeg: 160, bottomDeg: 95, topFloorDeg: 140, goodDepthDeg: 100, deeperMessage: 'Lower your chest further.' },
};

export function profileFor(slug: ExerciseSlug): ExerciseProfile {
  return PROFILES[slug];
}

/** The angle of the counted joint for this frame: the mean of the visible sides, or null if neither is visible. */
export function jointAngle(angles: PoseAngles | null, joint: 'knee' | 'elbow'): number | null {
  if (!angles) return null;
  const pair = joint === 'knee' ? [angles.left_knee_angle_deg, angles.right_knee_angle_deg] : [angles.left_elbow_angle_deg, angles.right_elbow_angle_deg];
  const seen = pair.filter((v): v is number => typeof v === 'number' && Number.isFinite(v));
  return seen.length ? seen.reduce((a, b) => a + b, 0) / seen.length : null;
}

const clamp01 = (v: number) => Math.min(1, Math.max(0, v));

/**
 * 0 = top of the movement (standing, arms extended), 1 = bottom (a full squat / chest low). Clamped.
 * `topDeg` is passed in so a caller can substitute the person's own observed top (see ``AdaptiveTop``).
 */
export function bodyPosition(angleDeg: number, bottomDeg: number, topDeg: number): number {
  if (!(topDeg > bottomDeg)) return 0;
  return clamp01((topDeg - angleDeg) / (topDeg - bottomDeg));
}

/**
 * People do not all read the same standing angle (camera height, slightly bent knees), and a top that is never reached would make
 * the form hints misfire. The top of the range is the highest angle seen in the last few
 * seconds, kept between the profile's floor and its default, so it follows the person without drifting down while
 * they hold a squat (the window is longer than a rep).
 */
export class AdaptiveTop {
  private readonly buckets: number[] = [];
  private bucketStart = Number.NEGATIVE_INFINITY;
  private readonly profile: ExerciseProfile;
  private readonly windowBuckets: number;
  private readonly bucketMs: number;

  constructor(profile: ExerciseProfile, windowBuckets = 8, bucketMs = 1000) {
    this.profile = profile;
    this.windowBuckets = windowBuckets;
    this.bucketMs = bucketMs;
  }

  update(angleDeg: number, nowMs: number): number {
    if (nowMs - this.bucketStart >= this.bucketMs) {
      this.buckets.push(angleDeg);
      if (this.buckets.length > this.windowBuckets) this.buckets.shift();
      this.bucketStart = nowMs;
    } else {
      this.buckets[this.buckets.length - 1] = Math.max(this.buckets[this.buckets.length - 1], angleDeg);
    }
    return this.top;
  }

  get top(): number {
    if (!this.buckets.length) return this.profile.topDeg; // nothing seen yet: the default
    return Math.min(this.profile.topDeg, Math.max(this.profile.topFloorDeg, Math.max(...this.buckets) - 2));
  }

  reset(): void {
    this.buckets.length = 0;
    this.bucketStart = Number.NEGATIVE_INFINITY;
  }
}

/** Feedback for one counted rep, from how deep it went. Descriptive, not a diagnosis. */
export function repFeedback(profile: ExerciseProfile, minAngleDeg: number): string {
  return minAngleDeg <= profile.goodDepthDeg ? 'Good rep!' : profile.deeperMessage;
}

export const MSG = {
  inView: 'Move into camera view.',
  standFully: 'Stand fully before the next rep.',
  backAligned: 'Keep your back aligned.',
} as const;

/**
 * Hints from the angle stream. At most one per ``cooldownMs``, so the screen never flickers.
 *  - "Stand fully before the next rep.": started going down again before getting back to (near) the top.
 *  - "Keep your back aligned." (push-ups only): the shoulder-hip-knee angle collapsed while the elbows were bending.
 *    A single camera cannot judge a squatter's spine reliably, so squats get no back hint.
 */
export class FormMonitor {
  private reachedBottom = false;
  private peakSinceBottom = Number.NEGATIVE_INFINITY;
  private lastTipAt = Number.NEGATIVE_INFINITY;
  private readonly profile: ExerciseProfile;
  private readonly cooldownMs: number;

  constructor(profile: ExerciseProfile, cooldownMs = 3000) {
    this.profile = profile;
    this.cooldownMs = cooldownMs;
  }

  update(angles: PoseAngles | null, nowMs: number, topDeg = this.profile.topDeg): string | null {
    const angle = jointAngle(angles, this.profile.joint);
    if (angle === null || angles === null) return null;

    let tip: string | null = null;
    const bottomZone = this.profile.bottomDeg + 10;
    if (angle <= bottomZone) {
      this.reachedBottom = true;
      this.peakSinceBottom = angle;
    } else if (this.reachedBottom) {
      this.peakSinceBottom = Math.max(this.peakSinceBottom, angle);
      const backedOffTheTop = this.peakSinceBottom < topDeg - 12;
      // a real rise (not noise) followed by a drop of 10+ degrees before getting near the top
      const goingDownAgain = this.peakSinceBottom >= bottomZone + 15 && angle <= this.peakSinceBottom - 10 && angle > bottomZone;
      if (angle >= topDeg - 12) {
        this.reachedBottom = false; // stood (near) fully: the next rep is a fresh start
      } else if (backedOffTheTop && goingDownAgain) {
        tip = MSG.standFully;
        this.reachedBottom = false;
      }
    }

    if (!tip && this.profile.slug === 'push-up') {
      const hips = [angles.left_hip_angle_deg, angles.right_hip_angle_deg].filter((v): v is number => typeof v === 'number');
      if (hips.length && hips.reduce((a, b) => a + b, 0) / hips.length < 150 && angle < 140) tip = MSG.backAligned;
    }

    if (tip && nowMs - this.lastTipAt < this.cooldownMs) return null;
    if (tip) this.lastTipAt = nowMs;
    return tip;
  }

  reset(): void {
    this.reachedBottom = false;
    this.peakSinceBottom = Number.NEGATIVE_INFINITY;
    this.lastTipAt = Number.NEGATIVE_INFINITY;
  }
}

/**
 * SIMULATED squat and push-up angles, for testing the whole pipeline without a camera or a person in front of it.
 * Synthetic data: it is not real tracking. Same idea as the backend's app/simulation.py.
 * Pure: no DOM, no React Native.
 */

import type { PoseAngles } from './angles';

export type SimulatedSquatOptions = {
  /** Per-rep depth: the angle at the bottom (smaller = deeper). One entry per rep. */
  depths: readonly number[];
  /** Which joint moves: the knee (squat, the default) or the elbow (push-up). */
  joint?: 'knee' | 'elbow';
  standingDeg?: number;
  /** Duration of one rep in milliseconds. */
  repMs?: number;
  frameMs?: number;
  restFrames?: number;
  confidence?: number;
};

/** One knee-angle frame per entry: rest, then each rep as a smooth stand -> depth -> stand curve, with rest between. */
export function simulatedSquatFrames(options: SimulatedSquatOptions): PoseAngles[] {
  const { depths, joint = 'knee', standingDeg = 170, repMs = 2400, frameMs = 33, restFrames = 10, confidence = 0.95 } = options;
  const frame = (angle: number): PoseAngles => {
    const left = Math.round((angle - 1) * 10) / 10; // a 2 degree left/right difference, like the backend simulator
    const right = Math.round((angle + 1) * 10) / 10;
    return {
      left_knee_angle_deg: joint === 'knee' ? left : null,
      right_knee_angle_deg: joint === 'knee' ? right : null,
      left_elbow_angle_deg: joint === 'elbow' ? left : null,
      right_elbow_angle_deg: joint === 'elbow' ? right : null,
      left_hip_angle_deg: null,
      right_hip_angle_deg: null,
      landmark_confidence: confidence,
    };
  };
  const out: PoseAngles[] = [];
  for (let i = 0; i < restFrames; i++) out.push(frame(standingDeg));
  const steps = Math.floor(repMs / frameMs);
  for (const depth of depths) {
    for (let i = 1; i <= steps; i++) {
      const phase = i / steps;
      out.push(frame(standingDeg - (standingDeg - depth) * 0.5 * (1 - Math.cos(2 * Math.PI * phase))));
    }
    for (let i = 0; i < restFrames; i++) out.push(frame(standingDeg));
  }
  return out;
}

/** Push-ups: the ELBOW angle goes from arms extended down to the given depth and back (same shape as the backend's). */
export function simulatedPushUpFrames(options: Omit<SimulatedSquatOptions, 'joint'>): PoseAngles[] {
  return simulatedSquatFrames({ standingDeg: 165, repMs: 1800, ...options, joint: 'elbow' });
}

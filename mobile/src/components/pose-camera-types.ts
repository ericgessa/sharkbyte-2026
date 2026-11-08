import type { Landmark, PoseAngles } from '@/pose/angles';

export type PoseFrameInfo = {
  /** Angles for this frame, or null when no joint was visible enough (nobody in view, or too close). */
  angles: PoseAngles | null;
  /** Normalized image landmarks, or null when no person was detected. */
  landmarks: Landmark[] | null;
  /** Processing rate, frames per second (smoothed over about a second). */
  fps: number;
};

export type PoseCameraProps = {
  /** Called for every processed camera frame. Keep it cheap (store the angles; don't set state on every call). */
  onFrame?: (info: PoseFrameInfo) => void;
  /** Optional colors for the Start/Stop camera button (defaults to the original blue). */
  tint?: { button: string; text: string };
};

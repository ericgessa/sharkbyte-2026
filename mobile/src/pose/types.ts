import type { Landmark } from './angles';

/** One detected person: normalized image landmarks (for drawing) and 3D world landmarks (for angles). */
export type PoseFrame = { landmarks: Landmark[]; worldLandmarks: Landmark[] };

export type PoseDetector = {
  /** GPU if the browser could use it, else CPU (slower, same results). */
  readonly delegate: 'GPU' | 'CPU';
  /** Run pose detection on a video element or image. `timestampMs` is bumped if it is not strictly increasing. */
  detect(source: unknown, timestampMs: number): PoseFrame | null;
  /** Everyone in view (up to the limit set with `setMaxPeople`), in no particular order. Web only. */
  detectMany?(source: unknown, timestampMs: number): PoseFrame[];
  /** How many people to look for (default 1). Raise it only while a screen needs it: more people cost more time. */
  setMaxPeople?(count: number): void;
  close(): void;
};

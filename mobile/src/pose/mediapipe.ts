/**
 * Native (iOS/Android) stand-in. MediaPipe pose tracking currently runs in the WEB browser only
 * (see mediapipe.web.ts); on native there is no detector yet. Metro picks mediapipe.web.ts on web.
 */

import type { PoseDetector, PoseFrame } from './types';

export const POSE_TRACKING_AVAILABLE = false;

const WEB_ONLY = 'Pose tracking runs in the web browser for now.';

export async function getPoseDetector(): Promise<PoseDetector> {
  throw new Error(WEB_ONLY);
}

export async function detectOnImageUrl(_url: string): Promise<PoseFrame | null> {
  throw new Error(WEB_ONLY);
}

import type { Landmark, PoseAngles } from '@/pose/angles';

import { Body } from './ui-grits';

export type PlayerFrame = { angles: PoseAngles | null; landmarks: Landmark[] };
export type MultiFrame = { players: [PlayerFrame | null, PlayerFrame | null]; fps: number };
export type MultiPoseCameraProps = { onFrame?: (frame: MultiFrame) => void };

/** Tracking two people at once runs in a desktop web browser for now. */
export function MultiPoseCamera(_props: MultiPoseCameraProps) {
  return <Body>Local 2-player runs in a desktop web browser for now. Open this app on your computer to play.</Body>;
}

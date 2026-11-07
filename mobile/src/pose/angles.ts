/**
 * Joint angles from MediaPipe pose landmarks.
 *
 * Pure functions: no DOM, no React Native, so they are unit-testable and reusable by any screen.
 * Angles are computed from MediaPipe's WORLD landmarks (3D, metres, origin at the hips) rather than the image
 * coordinates, so the result does not depend on how far the person is from the camera.
 *
 * Conventions match the backend contract (docs/event-contracts.md, `pose_update`): degrees 0-180, 180 = straight
 * limb, `null` for a joint that is not visible, plus one `landmark_confidence` for the frame.
 */

export type Point3 = { x: number; y: number; z: number };
export type Landmark = Point3 & { visibility?: number };

/** MediaPipe Pose landmark indices (the 33-point model). */
export const LM = {
  LEFT_SHOULDER: 11,
  RIGHT_SHOULDER: 12,
  LEFT_ELBOW: 13,
  RIGHT_ELBOW: 14,
  LEFT_WRIST: 15,
  RIGHT_WRIST: 16,
  LEFT_HIP: 23,
  RIGHT_HIP: 24,
  LEFT_KNEE: 25,
  RIGHT_KNEE: 26,
  LEFT_ANKLE: 27,
  RIGHT_ANKLE: 28,
} as const;

export type PoseAngles = {
  left_knee_angle_deg: number | null;
  right_knee_angle_deg: number | null;
  left_elbow_angle_deg: number | null;
  right_elbow_angle_deg: number | null;
  left_hip_angle_deg: number | null;
  right_hip_angle_deg: number | null;
  /** 0-1: how well the landmarks behind the reported angles were visible (mean visibility of those landmarks). */
  landmark_confidence: number;
};

type AngleKey = Exclude<keyof PoseAngles, 'landmark_confidence'>;

/** Each angle is measured AT the middle landmark, between the other two. */
const ANGLE_DEFS: Record<AngleKey, readonly [number, number, number]> = {
  left_knee_angle_deg: [LM.LEFT_HIP, LM.LEFT_KNEE, LM.LEFT_ANKLE],
  right_knee_angle_deg: [LM.RIGHT_HIP, LM.RIGHT_KNEE, LM.RIGHT_ANKLE],
  left_elbow_angle_deg: [LM.LEFT_SHOULDER, LM.LEFT_ELBOW, LM.LEFT_WRIST],
  right_elbow_angle_deg: [LM.RIGHT_SHOULDER, LM.RIGHT_ELBOW, LM.RIGHT_WRIST],
  left_hip_angle_deg: [LM.LEFT_SHOULDER, LM.LEFT_HIP, LM.LEFT_KNEE],
  right_hip_angle_deg: [LM.RIGHT_SHOULDER, LM.RIGHT_HIP, LM.RIGHT_KNEE],
};

const ANGLE_KEYS = Object.keys(ANGLE_DEFS) as AngleKey[];

/** Landmarks below this visibility are not trusted (the backend also ignores frames under 0.6 confidence). */
export const DEFAULT_MIN_VISIBILITY = 0.5;

/** The angle at `b` between the rays b->a and b->c, in degrees (0-180), or null if it cannot be computed. */
export function jointAngleDeg(a: Point3, b: Point3, c: Point3): number | null {
  const v1 = [a.x - b.x, a.y - b.y, a.z - b.z];
  const v2 = [c.x - b.x, c.y - b.y, c.z - b.z];
  if (![...v1, ...v2].every(Number.isFinite)) return null;
  const n1 = Math.hypot(v1[0], v1[1], v1[2]);
  const n2 = Math.hypot(v2[0], v2[1], v2[2]);
  if (!(n1 > 1e-9) || !(n2 > 1e-9)) return null; // two landmarks on top of each other: no direction
  const cos = (v1[0] * v2[0] + v1[1] * v2[1] + v1[2] * v2[2]) / (n1 * n2);
  return (Math.acos(Math.max(-1, Math.min(1, cos))) * 180) / Math.PI;
}

const round1 = (v: number) => Math.round(v * 10) / 10;

/**
 * Angles for one frame, or null when no angle could be computed (the backend rejects a `pose_update` with none).
 *
 * @param world       MediaPipe `worldLandmarks[0]` (3D, used for the geometry).
 * @param visibilityFrom  Where to read per-landmark `visibility` (defaults to `world`; MediaPipe provides it on both).
 *                    A landmark with no visibility value is treated as NOT visible rather than guessed.
 */
export function computePoseAngles(
  world: readonly Landmark[],
  visibilityFrom: readonly Landmark[] = world,
  minVisibility: number = DEFAULT_MIN_VISIBILITY,
): PoseAngles | null {
  const out: PoseAngles = {
    left_knee_angle_deg: null,
    right_knee_angle_deg: null,
    left_elbow_angle_deg: null,
    right_elbow_angle_deg: null,
    left_hip_angle_deg: null,
    right_hip_angle_deg: null,
    landmark_confidence: 0,
  };
  const used = new Set<number>();

  for (const key of ANGLE_KEYS) {
    const idx = ANGLE_DEFS[key];
    const pts = idx.map((i) => world[i]);
    if (pts.some((p) => p === undefined)) continue;
    if (idx.some((i) => (visibilityFrom[i]?.visibility ?? 0) < minVisibility)) continue;
    const angle = jointAngleDeg(pts[0], pts[1], pts[2]);
    if (angle === null) continue;
    out[key] = round1(angle);
    idx.forEach((i) => used.add(i));
  }

  if (used.size === 0) return null;
  let sum = 0;
  used.forEach((i) => {
    sum += visibilityFrom[i]?.visibility ?? 0;
  });
  out.landmark_confidence = Math.round(Math.min(1, Math.max(0, sum / used.size)) * 1000) / 1000;
  return out;
}

/**
 * MediaPipe Pose Landmarker in the web browser (Expo web).
 *
 * - The library is loaded at RUNTIME from the CDN, not bundled: Metro cannot bundle @mediapipe/tasks-vision (its code
 *   contains `import(<expression>)`, which Metro rejects and which no config option allows). The npm package stays
 *   installed for its TYPES only (`import type` is erased at build time). The web build is also server-rendered, so
 *   nothing browser-only may run when this module is merely imported.
 * - Library, WebAssembly runtime and package.json must all be the SAME version (a mismatch breaks loading).
 *   The model (about 6 MB, "lite") comes from Google's bucket. Everything is fetched by the browser at runtime,
 *   so the page needs internet access.
 * - Runs entirely on the user's device: no video or image ever leaves the browser.
 */

import type * as VisionTasks from '@mediapipe/tasks-vision';

import type { Landmark } from './angles';
import { LIBRARY_URL, MODEL_URL, WASM_BASE } from './mediapipe-config';
import type { PoseDetector, PoseFrame } from './types';

export { LIBRARY_URL, MEDIAPIPE_VERSION, MODEL_URL, WASM_BASE } from './mediapipe-config';

export const POSE_TRACKING_AVAILABLE = true;

// A native dynamic import that Metro does not try to resolve at build time.
const importFromUrl = new Function('url', 'return import(url)') as (url: string) => Promise<typeof VisionTasks>;

async function createPoseDetector(): Promise<PoseDetector> {
  const { FilesetResolver, PoseLandmarker } = await importFromUrl(LIBRARY_URL);
  const vision = await FilesetResolver.forVisionTasks(WASM_BASE);
  const make = (delegate: 'GPU' | 'CPU') =>
    PoseLandmarker.createFromOptions(vision, {
      baseOptions: { modelAssetPath: MODEL_URL, delegate },
      runningMode: 'VIDEO',
      numPoses: 1,
    });

  let delegate: 'GPU' | 'CPU' = 'GPU';
  let landmarker;
  try {
    landmarker = await make('GPU');
  } catch {
    delegate = 'CPU'; // no WebGL / GPU delegate unavailable: slower but works
    landmarker = await make('CPU');
  }

  let lastTs = -1;
  return {
    delegate,
    detect(source: unknown, timestampMs: number): PoseFrame | null {
      // VIDEO mode requires strictly increasing integer timestamps.
      const ts = Math.max(Math.floor(timestampMs), lastTs + 1);
      lastTs = ts;
      const result = landmarker.detectForVideo(source as HTMLVideoElement, ts);
      const landmarks = result.landmarks[0] as Landmark[] | undefined;
      const worldLandmarks = result.worldLandmarks[0] as Landmark[] | undefined;
      return landmarks && worldLandmarks ? { landmarks, worldLandmarks } : null;
    },
    detectMany(source: unknown, timestampMs: number): PoseFrame[] {
      const ts = Math.max(Math.floor(timestampMs), lastTs + 1);
      lastTs = ts;
      const result = landmarker.detectForVideo(source as HTMLVideoElement, ts);
      return result.landmarks.flatMap((landmarks, i) => {
        const worldLandmarks = result.worldLandmarks[i];
        return worldLandmarks ? [{ landmarks: landmarks as Landmark[], worldLandmarks: worldLandmarks as Landmark[] }] : [];
      });
    },
    setMaxPeople(count: number) {
      void landmarker.setOptions({ numPoses: Math.max(1, Math.floor(count)) });
    },
    close() {
      landmarker.close();
    },
  };
}

let shared: Promise<PoseDetector> | null = null;

/** One detector per page, created on first use and shared by everything that needs it. */
export function getPoseDetector(): Promise<PoseDetector> {
  if (!shared) {
    shared = createPoseDetector().catch((e) => {
      shared = null; // allow a retry after a failure (e.g. offline)
      throw e;
    });
  }
  return shared;
}

/** Detect a pose in a still image (handy without a camera). The image host must allow cross-origin use (CORS). */
export async function detectOnImageUrl(url: string): Promise<PoseFrame | null> {
  const detector = await getPoseDetector();
  const img = new window.Image();
  img.crossOrigin = 'anonymous';
  img.src = url;
  try {
    await img.decode();
  } catch {
    throw new Error('Could not load that image (wrong URL, or the host does not allow cross-origin use).');
  }
  return detector.detect(img, performance.now());
}

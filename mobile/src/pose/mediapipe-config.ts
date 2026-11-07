// Plain constants, safe on every platform. Kept out of mediapipe.web.ts, whose `new Function` crashes Hermes on native.

/** Must equal the version of @mediapipe/tasks-vision in package.json (a test checks this). */
export const MEDIAPIPE_VERSION = '1.2.2';
const CDN_BASE = `https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@${MEDIAPIPE_VERSION}`;
export const LIBRARY_URL = `${CDN_BASE}/vision_bundle.mjs`;
export const WASM_BASE = `${CDN_BASE}/wasm`;
export const MODEL_URL =
  'https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task';

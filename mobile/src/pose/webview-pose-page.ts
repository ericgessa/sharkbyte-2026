// HTML page that runs MediaPipe Pose inside a WebView on iOS/Android. It only does camera + model + skeleton
// overlay and posts raw landmarks; the app computes angles with the shared angles.ts.

import type { Landmark } from './angles';
import { LIBRARY_URL, MODEL_URL, WASM_BASE } from './mediapipe-config';
import { SKELETON } from './skeleton';

/** Messages the page posts to the app. Landmarks are packed as [x, y, z, visibility]. */
export type PoseWebViewMessage =
  | { t: 'status'; status: 'loading-model' | 'starting-camera' | 'running'; delegate?: 'GPU' | 'CPU' }
  | { t: 'error'; message: string }
  | { t: 'frame'; fps: number; lm: number[][] | null; wlm: number[][] | null };

export function parsePoseWebViewMessage(raw: string): PoseWebViewMessage | null {
  try {
    const m = JSON.parse(raw) as PoseWebViewMessage;
    return m && (m.t === 'status' || m.t === 'error' || m.t === 'frame') ? m : null;
  } catch {
    return null;
  }
}

export function unpackLandmarks(packed: number[][]): Landmark[] {
  return packed.map(([x, y, z, visibility]) => ({ x, y, z, visibility }));
}

const MAX_POST_FPS = 30;

export function buildPoseWebViewHtml(): string {
  const config = JSON.stringify({ LIBRARY_URL, WASM_BASE, MODEL_URL, SKELETON, MIN_POST_MS: 1000 / MAX_POST_FPS });
  return `<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<style>
  html, body { margin: 0; height: 100%; background: #111; overflow: hidden; }
  video, canvas { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; transform: scaleX(-1); }
</style>
</head>
<body>
<video id="video" playsinline webkit-playsinline muted autoplay></video>
<canvas id="canvas"></canvas>
<script type="module">
const CFG = ${config};
const post = (m) => window.ReactNativeWebView && window.ReactNativeWebView.postMessage(JSON.stringify(m));
const fail = (message) => post({ t: 'error', message: String(message) });
window.addEventListener('error', (e) => fail(e.message || 'script error'));
window.addEventListener('unhandledrejection', (e) => fail((e.reason && e.reason.message) || e.reason || 'error'));

function cameraError(e) {
  const n = e && e.name;
  if (n === 'NotAllowedError' || n === 'SecurityError') return 'Camera permission was denied. Allow camera access for Expo Go in iOS Settings and try again.';
  if (n === 'NotFoundError' || n === 'OverconstrainedError') return 'No camera was found on this device.';
  if (n === 'NotReadableError') return 'The camera is in use by another app.';
  return (e && e.message) || 'Could not start the camera.';
}

const pack = (lms) => lms.map((p) => [
  Math.round(p.x * 1e4) / 1e4, Math.round(p.y * 1e4) / 1e4, Math.round(p.z * 1e4) / 1e4,
  Math.round((p.visibility ?? 0) * 1e3) / 1e3,
]);

async function main() {
  if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    fail('This WebView cannot use the camera (no secure context or getUserMedia). iOS 14.5 or later is required.');
    return;
  }

  post({ t: 'status', status: 'loading-model' });
  const { FilesetResolver, PoseLandmarker } = await import(CFG.LIBRARY_URL);
  const vision = await FilesetResolver.forVisionTasks(CFG.WASM_BASE);
  const make = (delegate) => PoseLandmarker.createFromOptions(vision, {
    baseOptions: { modelAssetPath: CFG.MODEL_URL, delegate }, runningMode: 'VIDEO', numPoses: 1,
  });
  let delegate = 'GPU';
  let landmarker;
  try { landmarker = await make('GPU'); } catch { delegate = 'CPU'; landmarker = await make('CPU'); }

  post({ t: 'status', status: 'starting-camera', delegate });
  const video = document.getElementById('video');
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: 'user', width: { ideal: 640 }, height: { ideal: 480 } }, audio: false,
    });
  } catch (e) {
    fail(cameraError(e));
    return;
  }
  video.srcObject = stream;
  await video.play();
  post({ t: 'status', status: 'running', delegate });

  const canvas = document.getElementById('canvas');
  const ctx = canvas.getContext('2d');
  let lastVideoTime = -1, lastTs = -1, lastPost = -Infinity;
  let windowStart = performance.now(), windowFrames = 0, fps = 0;

  const draw = (lm) => {
    if (canvas.width !== video.videoWidth) canvas.width = video.videoWidth;
    if (canvas.height !== video.videoHeight) canvas.height = video.videoHeight;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!lm) return;
    const seen = (i) => (lm[i].visibility ?? 0) >= 0.5;
    const px = (i) => [lm[i].x * canvas.width, lm[i].y * canvas.height];
    ctx.lineWidth = 3; ctx.strokeStyle = '#3c87f7';
    for (const [a, b] of CFG.SKELETON) {
      if (!seen(a) || !seen(b)) continue;
      ctx.beginPath(); ctx.moveTo(...px(a)); ctx.lineTo(...px(b)); ctx.stroke();
    }
    ctx.fillStyle = '#ffd400';
    for (const [a, b] of CFG.SKELETON) for (const i of [a, b]) {
      if (!seen(i)) continue;
      const [x, y] = px(i);
      ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fill();
    }
  };

  const tick = () => {
    if (video.readyState >= 2 && video.currentTime !== lastVideoTime) {
      lastVideoTime = video.currentTime;
      const now = performance.now();
      let lm = null, wlm = null;
      try {
        const ts = Math.max(Math.floor(now), lastTs + 1); // VIDEO mode needs strictly increasing timestamps
        lastTs = ts;
        const r = landmarker.detectForVideo(video, ts);
        lm = r.landmarks[0] || null;
        wlm = r.worldLandmarks[0] || null;
      } catch {
        // one bad frame must not stop tracking
      }
      windowFrames += 1;
      if (now - windowStart >= 1000) {
        fps = Math.round((windowFrames * 1000) / (now - windowStart));
        windowFrames = 0; windowStart = now;
      }
      draw(lm);
      if (now - lastPost >= CFG.MIN_POST_MS) {
        lastPost = now;
        post({ t: 'frame', fps, lm: lm && wlm ? pack(lm) : null, wlm: lm && wlm ? pack(wlm) : null });
      }
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

main().catch((e) => fail((e && e.message) || e));
</script>
</body>
</html>`;
}

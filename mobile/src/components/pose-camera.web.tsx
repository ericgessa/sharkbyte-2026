import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { Pressable, StyleSheet, View } from 'react-native';

import { ThemedText } from '@/components/themed-text';
import { Spacing } from '@/constants/theme';
import { computePoseAngles } from '@/pose/angles';
import { SKELETON } from '@/pose/skeleton';
import { usePoseDetector } from '@/pose/use-pose-detector';

import type { PoseCameraProps } from './pose-camera-types';

export type { PoseCameraProps, PoseFrameInfo } from './pose-camera-types';

type CameraState = 'off' | 'starting' | 'on' | 'error';

function cameraErrorMessage(e: unknown): string {
  const name = e instanceof DOMException ? e.name : '';
  if (name === 'NotAllowedError' || name === 'SecurityError') return 'Camera permission was denied. Allow it in the browser address bar and try again.';
  if (name === 'NotFoundError' || name === 'OverconstrainedError') return 'No camera was found on this device.';
  if (name === 'NotReadableError') return 'The camera is in use by another app.';
  return e instanceof Error ? e.message : 'Could not start the camera.';
}

/**
 * Webcam + MediaPipe pose tracking in the browser. All processing happens on this device: video never leaves it.
 * Draws the detected skeleton over a mirrored ("selfie") view and reports angles for every frame via `onFrame`.
 */
export function PoseCamera({ onFrame }: PoseCameraProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const onFrameRef = useRef(onFrame);
  useEffect(() => {
    onFrameRef.current = onFrame; // kept in a ref so the detection loop never restarts when the callback changes
  });

  const model = usePoseDetector();
  const [camera, setCamera] = useState<CameraState>('off');
  const [cameraError, setCameraError] = useState<string | null>(null);

  const stop = () => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    if (videoRef.current) videoRef.current.srcObject = null;
    setCamera('off');
  };

  const start = async () => {
    setCameraError(null);
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      setCamera('error');
      setCameraError('The camera needs a secure page: open this app at http://localhost or over https. A plain http address on your network is blocked by the browser.');
      return;
    }
    setCamera('starting');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: 'user', width: { ideal: 640 }, height: { ideal: 480 } },
        audio: false,
      });
      streamRef.current = stream;
      const video = videoRef.current;
      if (!video) {
        stream.getTracks().forEach((t) => t.stop());
        return;
      }
      video.srcObject = stream;
      await video.play();
      setCamera('on');
    } catch (e) {
      setCamera('error');
      setCameraError(cameraErrorMessage(e));
    }
  };

  // Always release the camera when leaving the screen.
  useEffect(() => stop, []);

  // The detection loop: one pass per new video frame.
  useEffect(() => {
    const detector = model.detector;
    if (camera !== 'on' || !detector) return;
    let alive = true;
    let raf = 0;
    let lastVideoTime = -1;
    let windowStart = performance.now();
    let windowFrames = 0;
    let fps = 0;

    const tick = () => {
      if (!alive) return;
      const video = videoRef.current;
      const canvas = canvasRef.current;
      if (video && canvas && video.readyState >= 2 && video.currentTime !== lastVideoTime) {
        lastVideoTime = video.currentTime;
        const now = performance.now();
        let landmarks = null;
        let angles = null;
        try {
          const frame = detector.detect(video, now);
          if (frame) {
            landmarks = frame.landmarks;
            angles = computePoseAngles(frame.worldLandmarks, frame.landmarks);
          }
        } catch {
          // a single bad frame must not stop tracking
        }

        windowFrames += 1;
        if (now - windowStart >= 1000) {
          fps = Math.round((windowFrames * 1000) / (now - windowStart));
          windowFrames = 0;
          windowStart = now;
        }

        if (canvas.width !== video.videoWidth) canvas.width = video.videoWidth;
        if (canvas.height !== video.videoHeight) canvas.height = video.videoHeight;
        const ctx = canvas.getContext('2d');
        if (ctx) {
          ctx.clearRect(0, 0, canvas.width, canvas.height);
          if (landmarks) {
            const px = (i: number) => [landmarks![i].x * canvas.width, landmarks![i].y * canvas.height] as const;
            const seen = (i: number) => (landmarks![i].visibility ?? 0) >= 0.5;
            ctx.lineWidth = 3;
            ctx.strokeStyle = '#3c87f7';
            for (const [a, b] of SKELETON) {
              if (!seen(a) || !seen(b)) continue;
              ctx.beginPath();
              ctx.moveTo(...px(a));
              ctx.lineTo(...px(b));
              ctx.stroke();
            }
            ctx.fillStyle = '#ffd400';
            for (const [a, b] of SKELETON) {
              for (const i of [a, b]) {
                if (!seen(i)) continue;
                const [x, y] = px(i);
                ctx.beginPath();
                ctx.arc(x, y, 4, 0, Math.PI * 2);
                ctx.fill();
              }
            }
          }
        }
        onFrameRef.current?.({ angles, landmarks, fps });
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => {
      alive = false;
      cancelAnimationFrame(raf);
    };
  }, [camera, model.detector]);

  const mirror: CSSProperties = { transform: 'scaleX(-1)' }; // selfie view
  const fill: CSSProperties = { position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover', ...mirror };

  const modelText =
    model.status === 'loading'
      ? 'Loading the pose model (about 6 MB)...'
      : model.status === 'error'
        ? `Pose model failed to load: ${model.error}`
        : `Pose model ready (${model.detector.delegate})`;

  return (
    <View style={styles.wrap}>
      <div style={{ position: 'relative', width: '100%', maxWidth: 640, aspectRatio: '4 / 3', background: '#111', borderRadius: 12, overflow: 'hidden' }}>
        <video ref={videoRef} playsInline muted style={fill} />
        <canvas ref={canvasRef} style={fill} />
        {camera !== 'on' && (
          <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#bbb', fontSize: 14, padding: 16, textAlign: 'center' }}>
            {camera === 'starting' ? 'Starting the camera...' : 'Camera is off'}
          </div>
        )}
      </div>

      <ThemedText type="small" themeColor="textSecondary" testID="pose-model-status">
        {modelText}
      </ThemedText>
      {cameraError && (
        <ThemedText type="small" testID="pose-camera-error" style={styles.error}>
          {cameraError}
        </ThemedText>
      )}

      <Pressable
        accessibilityRole="button"
        onPress={camera === 'on' ? stop : start}
        disabled={camera === 'starting' || model.status === 'error'}
        style={({ pressed }) => [styles.button, pressed && styles.pressed]}>
        <ThemedText type="smallBold" style={styles.buttonText}>
          {camera === 'on' ? 'Stop camera' : 'Start camera'}
        </ThemedText>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { gap: Spacing.two, alignItems: 'flex-start', alignSelf: 'stretch' },
  button: { backgroundColor: '#3c87f7', borderRadius: Spacing.two, paddingVertical: Spacing.two, paddingHorizontal: Spacing.three },
  buttonText: { color: '#ffffff' },
  pressed: { opacity: 0.7 },
  error: { color: '#d93025' },
});

import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { Pressable, Text, View } from 'react-native';

import { computePoseAngles, type Landmark, type PoseAngles } from '@/pose/angles';
import { SKELETON } from '@/pose/skeleton';
import { usePoseDetector } from '@/pose/use-pose-detector';
import { assignPlayers, screenCenterX } from '@/games/people';
import { GG as G } from '@/games/theme';

export type PlayerFrame = { angles: PoseAngles | null; landmarks: Landmark[] };
export type MultiFrame = { players: [PlayerFrame | null, PlayerFrame | null]; fps: number };
export type MultiPoseCameraProps = { onFrame?: (frame: MultiFrame) => void };

const COLORS = [G.green, G.blue] as const;

function cameraError(e: unknown): string {
  const name = e instanceof DOMException ? e.name : '';
  if (name === 'NotAllowedError' || name === 'SecurityError') return 'Camera permission was denied. Allow it in the browser address bar and try again.';
  if (name === 'NotFoundError' || name === 'OverconstrainedError') return 'No camera was found on this device.';
  if (name === 'NotReadableError') return 'The camera is in use by another app.';
  return e instanceof Error ? e.message : 'Could not start the camera.';
}

/**
 * One webcam, two people. MediaPipe looks for two bodies; the person on the left of the (mirrored) preview is
 * Player 1 and the one on the right is Player 2, each drawn in their own colour. Everything runs in this browser.
 */
export function MultiPoseCamera({ onFrame }: MultiPoseCameraProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const onFrameRef = useRef(onFrame);
  useEffect(() => {
    onFrameRef.current = onFrame;
  });
  const model = usePoseDetector();
  const [camera, setCamera] = useState<'off' | 'starting' | 'on' | 'error'>('off');
  const [error, setError] = useState<string | null>(null);

  const stop = () => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    if (videoRef.current) videoRef.current.srcObject = null;
    setCamera('off');
  };

  const start = async () => {
    setError(null);
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      setCamera('error');
      setError('The camera needs a secure page: open this app at http://localhost or over https.');
      return;
    }
    setCamera('starting');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user', width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false });
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
      setError(cameraError(e));
    }
  };

  useEffect(() => stop, []);

  // Look for two people only while this camera is on; back to one afterwards so other screens stay as fast as before.
  useEffect(() => {
    const detector = model.detector;
    if (camera !== 'on' || !detector) return;
    detector.setMaxPeople?.(2);
    return () => detector.setMaxPeople?.(1);
  }, [camera, model.detector]);

  useEffect(() => {
    const detector = model.detector;
    if (camera !== 'on' || !detector) return;
    if (!detector.detectMany) return; // reported on screen (see `unsupported` below)
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
        let people: { landmarks: Landmark[]; worldLandmarks: Landmark[] }[] = [];
        try {
          people = detector.detectMany!(video, now);
        } catch {
          // one bad frame must not stop tracking
        }
        const [left, right] = assignPlayers(people.map((p) => screenCenterX(p.landmarks)));
        const players: [PlayerFrame | null, PlayerFrame | null] = [null, null];
        [left, right].forEach((index, slot) => {
          if (index === null) return;
          const p = people[index];
          players[slot as 0 | 1] = { angles: computePoseAngles(p.worldLandmarks, p.landmarks), landmarks: p.landmarks };
        });

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
          players.forEach((pf, slot) => {
            if (!pf) return;
            const lms = pf.landmarks;
            const px = (i: number) => [lms[i].x * canvas.width, lms[i].y * canvas.height] as const;
            const seen = (i: number) => (lms[i].visibility ?? 0) >= 0.5;
            ctx.lineWidth = 4;
            ctx.strokeStyle = COLORS[slot];
            for (const [a, b] of SKELETON) {
              if (!seen(a) || !seen(b)) continue;
              ctx.beginPath();
              ctx.moveTo(...px(a));
              ctx.lineTo(...px(b));
              ctx.stroke();
            }
          });
        }
        onFrameRef.current?.({ players, fps });
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => {
      alive = false;
      cancelAnimationFrame(raf);
    };
  }, [camera, model.detector]);

  const unsupported = model.status === 'ready' && !model.detector.detectMany;
  const fill: CSSProperties = { position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover', transform: 'scaleX(-1)' };
  const modelText = model.status === 'loading' ? 'Loading the pose model (about 6 MB)...' : model.status === 'error' ? `Pose model failed to load: ${model.error}` : `Pose model ready (${model.detector.delegate})`;

  return (
    <View style={{ gap: 8, alignSelf: 'stretch' }}>
      <div style={{ position: 'relative', width: '100%', aspectRatio: '16 / 9', background: '#111', borderRadius: 12, overflow: 'hidden' }}>
        <video ref={videoRef} playsInline muted style={fill} />
        <canvas ref={canvasRef} style={fill} />
        <div style={{ position: 'absolute', top: 0, bottom: 0, left: '50%', width: 0, borderLeft: '2px dashed rgba(255,255,255,0.35)' }} />
        <div style={{ position: 'absolute', top: 8, left: 10, color: G.green, fontWeight: 800, fontSize: 13, letterSpacing: 1 }}>PLAYER 1</div>
        <div style={{ position: 'absolute', top: 8, right: 10, color: G.blue, fontWeight: 800, fontSize: 13, letterSpacing: 1 }}>PLAYER 2</div>
        {camera !== 'on' && (
          <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#bbb', fontSize: 14, padding: 16, textAlign: 'center' }}>
            {camera === 'starting' ? 'Starting the camera...' : 'Camera is off'}
          </div>
        )}
      </div>
      <Text style={{ color: G.muted, fontSize: 13 }} testID="pose-model-status">
        {modelText}
      </Text>
      {(error || unsupported) && (
        <Text style={{ color: G.red, fontSize: 13 }} testID="pose-camera-error">
          {error ?? 'This browser build cannot track two people.'}
        </Text>
      )}
      <Pressable
        accessibilityRole="button"
        onPress={camera === 'on' ? stop : start}
        disabled={camera === 'starting' || model.status === 'error'}
        testID="multi-camera-toggle"
        style={({ pressed }) => ({ alignSelf: 'flex-start', backgroundColor: G.green, borderRadius: 10, paddingVertical: 8, paddingHorizontal: 16, opacity: pressed ? 0.7 : 1 })}>
        <Text style={{ color: G.greenInk, fontWeight: '800', fontSize: 14 }}>{camera === 'on' ? 'Stop camera' : 'Start camera'}</Text>
      </Pressable>
    </View>
  );
}

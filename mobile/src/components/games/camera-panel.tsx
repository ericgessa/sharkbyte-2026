import { useCallback, useEffect, useRef, useState } from 'react';
import { View } from 'react-native';

import { PoseCamera } from '@/components/pose-camera';
import { Grits } from '@/constants/grits';
import type { PoseFrameInfo } from '@/components/pose-camera-types';
import type { PoseAngles } from '@/pose/angles';
import { jointAngle, profileFor, type ExerciseSlug } from '@/games/exercise';

import { Badge, Body } from './ui-grits';

export type CameraStatus = 'off' | 'no-person' | 'tracking';

/**
 * Turns the camera's frame stream into one status for the screen ("camera off" / "no person" / "tracking") and hands
 * every frame's angles to `onAngles`. The camera itself is started with the button inside PoseCamera; the model is
 * loaded once and shared (see pose/mediapipe), so mounting this in two screens never starts a second detector.
 */
export function useCameraFeed(exercise: ExerciseSlug, onAngles?: (angles: PoseAngles | null) => void) {
  const joint = profileFor(exercise).joint;
  const [status, setStatus] = useState<CameraStatus>('off');
  const [fps, setFps] = useState(0);
  const lastFrameAt = useRef(0);
  const lastSeenAt = useRef(0);
  const fpsRef = useRef(0);
  const cb = useRef(onAngles);
  useEffect(() => {
    cb.current = onAngles;
  });

  const onFrame = useCallback(
    (info: PoseFrameInfo) => {
      const now = Date.now();
      lastFrameAt.current = now;
      fpsRef.current = info.fps;
      if (jointAngle(info.angles, joint) !== null) lastSeenAt.current = now;
      cb.current?.(info.angles);
    },
    [joint],
  );

  useEffect(() => {
    const id = setInterval(() => {
      const now = Date.now();
      const next: CameraStatus = now - lastFrameAt.current > 1500 ? 'off' : now - lastSeenAt.current > 600 ? 'no-person' : 'tracking';
      setStatus((prev) => (prev === next ? prev : next));
      setFps((prev) => (prev === fpsRef.current ? prev : fpsRef.current));
    }, 250);
    return () => clearInterval(id);
  }, []);

  return { status, fps, onFrame };
}

export function CameraBadge({ status, fps }: { status: CameraStatus; fps?: number }) {
  if (status === 'tracking') return <Badge label={`Camera: tracking${fps ? ` · ${fps} fps` : ''}`} tone="green" testID="camera-status" />;
  if (status === 'no-person') return <Badge label="Camera on: move into view" tone="amber" testID="camera-status" />;
  return <Badge label="Camera: off" tone="red" testID="camera-status" />;
}

const PLACEMENT: Record<ExerciseSlug, string> = {
  squat: 'Stand 2 to 3 metres back so your hips, knees and feet are all in view. Side-on works best.',
  'push-up': 'Put the camera low and to your side so your arm and body line are in view.',
};

/** The live camera preview with its status. Video stays on this device: only joint angles are ever sent anywhere. */
export function CameraPanel({ exercise, status, fps, onFrame, width = 360 }: { exercise: ExerciseSlug; status: CameraStatus; fps?: number; onFrame: (info: PoseFrameInfo) => void; width?: number }) {
  return (
    <View style={{ width, maxWidth: '100%', gap: 8 }}>
      <CameraBadge status={status} fps={fps} />
      <PoseCamera onFrame={onFrame} />
      <Body style={{ fontSize: 13, color: Grits.inkSoft }}>{PLACEMENT[exercise]} Your video never leaves this device.</Body>
    </View>
  );
}

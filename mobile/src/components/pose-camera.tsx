import { useEffect, useMemo, useRef, useState } from 'react';
import { Pressable, StyleSheet, View } from 'react-native';
import { WebView, type WebViewMessageEvent } from 'react-native-webview';

import { ThemedText } from '@/components/themed-text';
import { Spacing } from '@/constants/theme';
import { G } from '@/games/theme';
import { computePoseAngles } from '@/pose/angles';
import { buildPoseWebViewHtml, parsePoseWebViewMessage, unpackLandmarks } from '@/pose/webview-pose-page';

import type { PoseCameraProps } from './pose-camera-types';

export type { PoseCameraProps, PoseFrameInfo } from './pose-camera-types';

type CameraState = 'off' | 'loading-model' | 'starting-camera' | 'running' | 'error';

const STATUS_TEXT: Record<CameraState, string> = {
  off: 'Camera is off',
  'loading-model': 'Loading the pose model (about 6 MB)...',
  'starting-camera': 'Starting the camera...',
  running: '',
  error: 'Camera is off',
};

/** iOS/Android: MediaPipe in a WebView (works in Expo Go). Same props as pose-camera.web.tsx. */
export function PoseCamera({ onFrame, tint }: PoseCameraProps) {
  const html = useMemo(() => buildPoseWebViewHtml(), []);
  const [state, setState] = useState<CameraState>('off');
  const [delegate, setDelegate] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const onFrameRef = useRef(onFrame);
  useEffect(() => {
    onFrameRef.current = onFrame;
  });

  // The WebView only exists while the camera is on, so stopping or leaving releases the camera.
  const on = state !== 'off' && state !== 'error';

  const onMessage = (e: WebViewMessageEvent) => {
    const m = parsePoseWebViewMessage(e.nativeEvent.data);
    if (!m) return;
    if (m.t === 'status') {
      setState(m.status);
      if (m.delegate) setDelegate(m.delegate);
    } else if (m.t === 'error') {
      setState('error');
      setError(m.message);
    } else {
      const landmarks = m.lm ? unpackLandmarks(m.lm) : null;
      const world = m.wlm ? unpackLandmarks(m.wlm) : null;
      const angles = landmarks && world ? computePoseAngles(world, landmarks) : null;
      onFrameRef.current?.({ angles, landmarks, fps: m.fps });
    }
  };

  const start = () => {
    setError(null);
    setState('loading-model');
  };

  return (
    <View style={styles.wrap}>
      <View style={styles.preview}>
        {on && (
          <WebView
            style={styles.webview}
            // An https base URL makes the page a secure context, which getUserMedia requires.
            source={{ html, baseUrl: 'https://localhost/' }}
            originWhitelist={['*']}
            javaScriptEnabled
            allowsInlineMediaPlayback
            mediaPlaybackRequiresUserAction={false}
            mediaCapturePermissionGrantType="grant"
            scrollEnabled={false}
            bounces={false}
            onMessage={onMessage}
            onError={(e) => {
              setState('error');
              setError(e.nativeEvent.description || 'The camera view failed to load.');
            }}
          />
        )}
        {state !== 'running' && (
          <View style={styles.overlay} pointerEvents="none">
            <ThemedText type="small" style={styles.overlayText}>
              {STATUS_TEXT[state]}
            </ThemedText>
          </View>
        )}
      </View>

      {state === 'running' && delegate && (
        <ThemedText type="small" themeColor="textSecondary" testID="pose-model-status">
          Pose model ready ({delegate})
        </ThemedText>
      )}
      {error && (
        <ThemedText type="small" testID="pose-camera-error" style={styles.error}>
          {error}
        </ThemedText>
      )}

      <Pressable
        accessibilityRole="button"
        onPress={on ? () => setState('off') : start}
        style={({ pressed }) => [styles.button, tint && { backgroundColor: tint.button, borderRadius: 999 }, pressed && styles.pressed]}>
        <ThemedText type="smallBold" style={[styles.buttonText, tint && { color: tint.text }]}>
          {on ? 'Stop camera' : 'Start camera'}
        </ThemedText>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { gap: Spacing.two, alignItems: 'flex-start', alignSelf: 'stretch' },
  preview: { width: '100%', aspectRatio: 3 / 4, backgroundColor: G.panel, borderRadius: 12, overflow: 'hidden' },
  webview: { flex: 1, backgroundColor: G.panel },
  overlay: { ...StyleSheet.absoluteFill, alignItems: 'center', justifyContent: 'center', padding: Spacing.three },
  overlayText: { color: G.muted, textAlign: 'center' },
  button: { backgroundColor: G.green, borderRadius: 12, paddingVertical: 10, paddingHorizontal: 18 },
  buttonText: { color: G.greenInk },
  pressed: { opacity: 0.7 },
  error: { color: G.red },
});

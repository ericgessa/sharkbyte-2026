import { useEffect, useState } from 'react';

import { getPoseDetector } from './mediapipe';
import type { PoseDetector } from './types';

export type DetectorState =
  | { status: 'loading'; detector: null; error: null }
  | { status: 'ready'; detector: PoseDetector; error: null }
  | { status: 'error'; detector: null; error: string };

/** Loads the shared pose detector once the screen is on the client (never during server rendering). */
export function usePoseDetector(): DetectorState {
  const [state, setState] = useState<DetectorState>({ status: 'loading', detector: null, error: null });

  useEffect(() => {
    let alive = true;
    getPoseDetector().then(
      (detector) => alive && setState({ status: 'ready', detector, error: null }),
      (e: unknown) => alive && setState({ status: 'error', detector: null, error: e instanceof Error ? e.message : String(e) }),
    );
    return () => {
      alive = false;
    };
  }, []);

  return state;
}

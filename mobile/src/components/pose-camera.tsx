import { ThemedText } from '@/components/themed-text';

import type { PoseCameraProps } from './pose-camera-types';

export type { PoseCameraProps, PoseFrameInfo } from './pose-camera-types';

/** iOS/Android: camera pose tracking is web-only for now (see pose-camera.web.tsx). */
export function PoseCamera(_props: PoseCameraProps) {
  return <ThemedText type="small">Camera pose tracking currently runs in the web browser only.</ThemedText>;
}

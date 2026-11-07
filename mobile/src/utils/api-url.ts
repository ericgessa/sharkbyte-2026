import Constants from 'expo-constants';
import { Platform } from 'react-native';

const BACKEND_PORT = 8000;

/** "192.168.1.20:8081" -> "192.168.1.20". */
export function hostFromHostUri(hostUri: string | null | undefined): string | null {
  if (!hostUri) return null;
  return hostUri.replace(/^[a-z]+:\/\//i, '').split('/')[0].replace(/:\d+$/, '') || null;
}

// Assumes the backend runs on the same computer as the Expo dev server. Set EXPO_PUBLIC_API_URL to override
// (backend elsewhere, a tunnel, or a production build).
function resolveApiUrl(): string {
  const override = process.env.EXPO_PUBLIC_API_URL?.trim();
  if (override) return override.replace(/\/+$/, '');
  const host =
    Platform.OS === 'web'
      ? typeof window !== 'undefined' && window.location.hostname
      : hostFromHostUri(Constants.expoConfig?.hostUri);
  return `http://${host || 'localhost'}:${BACKEND_PORT}`;
}

let cached: string | null = null;

export function getApiUrl(): string {
  cached ??= resolveApiUrl();
  return cached;
}

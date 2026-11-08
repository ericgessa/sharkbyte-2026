/**
 * Talking to the SharkByte backend: REST calls and the authenticated workout WebSocket.
 * Follows docs/frontend-integration.md. Pure TypeScript on top of the standard `fetch` / `WebSocket` globals, which
 * exist on web and React Native alike (and can be injected for tests). No DOM, no React Native imports.
 *
 * Authentication: every call needs the signed-in user's Supabase ACCESS TOKEN (not the publishable key).
 */

import type { PoseUpdate, ServerEvent, UserSignal } from './protocol';
import { parseServerEvent } from './protocol';
import type { SensorUpdate } from './wearable';

export type CreatedWorkout = {
  session_id: string;
  exercise: string;
  analysis_supported: boolean;
  status: string;
};

/** http -> ws, https -> wss, no trailing slash. */
export function toWsUrl(apiUrl: string): string {
  return apiUrl.replace(/\/+$/, '').replace(/^http/i, 'ws');
}

async function apiError(res: Response): Promise<Error> {
  let detail = '';
  try {
    const body: unknown = await res.json();
    const d = (body as { detail?: unknown }).detail;
    detail = typeof d === 'string' ? d : Array.isArray(d) ? String((d[0] as { msg?: unknown })?.msg ?? '') : '';
  } catch {
    // no JSON body
  }
  return new Error(`${res.status}${detail ? ` ${detail}` : ''}`);
}

function authHeaders(accessToken: string): Record<string, string> {
  return { Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/json' };
}

export async function createWorkout(
  apiUrl: string,
  accessToken: string,
  exercise = 'squat',
  fetchImpl: typeof fetch = fetch,
): Promise<CreatedWorkout> {
  const res = await fetchImpl(`${apiUrl.replace(/\/+$/, '')}/api/workouts`, {
    method: 'POST',
    headers: authHeaders(accessToken),
    body: JSON.stringify({ exercise }),
  });
  if (!res.ok) throw await apiError(res);
  return (await res.json()) as CreatedWorkout;
}

export async function finishWorkout(
  apiUrl: string,
  accessToken: string,
  sessionId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<{ total_reps: number; status: string }> {
  const res = await fetchImpl(`${apiUrl.replace(/\/+$/, '')}/api/workouts/${sessionId}/finish`, {
    method: 'POST',
    headers: authHeaders(accessToken),
  });
  if (!res.ok) throw await apiError(res);
  return (await res.json()) as { total_reps: number; status: string };
}

/** The end-of-workout report. `source` is "agent" when Gemini wrote it, "rules" for the deterministic fallback. */
export type CoachReport = { summary: string; source: 'agent' | 'rules' };

/**
 * The end-of-workout report. Call once, after `finishWorkout`. With Gemini it can take up to ~20 s (the server falls
 * back to the rules report on its own), so show the stats first. Limited to about 6 calls a minute (429).
 */
export async function getCoachReport(
  apiUrl: string,
  accessToken: string,
  sessionId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<CoachReport> {
  const res = await fetchImpl(`${apiUrl.replace(/\/+$/, '')}/api/workouts/${sessionId}/coach-summary`, {
    headers: authHeaders(accessToken),
  });
  if (!res.ok) throw await apiError(res);
  return (await res.json()) as CoachReport;
}

export type SocketHandlers = {
  /** Every parsed server event (including `session_ready`). */
  onEvent: (event: ServerEvent) => void;
  /** The socket closed. 4401 = token rejected/expired, 4404 = workout not found, 4409 = finished, 4410 = replaced. */
  onClose?: (code: number) => void;
  onError?: () => void;
};

/**
 * One workout's live connection. The first message MUST be `auth` (the server enforces it); frames are only sent
 * once the server has answered `session_ready`.
 */
export class WorkoutSocket {
  private ws: WebSocket | null = null;
  private ready = false;
  private readonly url: string;
  private readonly accessToken: string;
  private readonly handlers: SocketHandlers;
  private readonly WebSocketImpl: typeof WebSocket;

  constructor(
    apiUrl: string,
    sessionId: string,
    accessToken: string,
    handlers: SocketHandlers,
    WebSocketImpl: typeof WebSocket = WebSocket,
  ) {
    this.url = `${toWsUrl(apiUrl)}/ws/workouts/${sessionId}`;
    this.accessToken = accessToken;
    this.handlers = handlers;
    this.WebSocketImpl = WebSocketImpl;
  }

  get isReady(): boolean {
    return this.ready && this.ws !== null && this.ws.readyState === 1; // 1 = OPEN
  }

  connect(): void {
    const ws = new this.WebSocketImpl(this.url);
    this.ws = ws;
    ws.onopen = () => {
      ws.send(JSON.stringify({ schema_version: 1, type: 'auth', access_token: this.accessToken }));
    };
    ws.onmessage = (m: MessageEvent) => {
      const event = typeof m.data === 'string' ? parseServerEvent(m.data) : null;
      if (!event) return;
      if (event.type === 'session_ready') this.ready = true;
      this.handlers.onEvent(event);
    };
    ws.onclose = (e: CloseEvent) => {
      this.ready = false;
      this.handlers.onClose?.(e.code);
    };
    ws.onerror = () => this.handlers.onError?.();
  }

  /** false if not connected/authenticated yet (the frame is simply dropped; the next one will do). */
  sendPose(update: PoseUpdate): boolean {
    if (!this.isReady) return false;
    this.ws!.send(JSON.stringify(update));
    return true;
  }

  /** Spine wearable data, relayed over this same socket (the server allows one socket per workout). */
  sendSensor(update: SensorUpdate): boolean {
    if (!this.isReady) return false;
    this.ws!.send(JSON.stringify(update));
    return true;
  }

  /** pause / resume / pain. Pain stops technique coaching for the rest of the workout (server-side). */
  sendSignal(sessionId: string, signal: UserSignal): boolean {
    if (!this.isReady) return false;
    this.ws!.send(JSON.stringify({ schema_version: 1, type: 'user_signal', session_id: sessionId, signal }));
    return true;
  }

  close(): void {
    this.ready = false;
    this.ws?.close();
    this.ws = null;
  }
}

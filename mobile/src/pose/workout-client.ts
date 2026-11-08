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
export type CoachReport = {
  summary: string;
  source: 'agent' | 'rules';
  /** true: this report is stored in Supabase (written once when the workout finished), so reopening it is instant. */
  saved?: boolean;
  /** When the saved report was written (ISO), or null if it is not saved. */
  generated_at?: string | null;
};

/**
 * The end-of-workout report. The server writes it once, when the workout finishes, and saves it, so after that this just
 * returns the saved report. Right after `finishWorkout` it may still be being written (Gemini can take up to ~20 s; the
 * server falls back to the rules report on its own), so show the stats first. Limited to about 6 calls a minute (429).
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

/** One exercise's totals from GET /api/stats (completed workouts only). */
export type ExerciseStats = {
  exercise: string;
  workouts: number;
  total_reps: number;
  best_workout_reps: number;
  last_workout_at: string | null;
};

/** The caller's progress across workouts (only the parts the app uses so far). New users get zeros. */
export async function getStats(
  apiUrl: string,
  accessToken: string,
  fetchImpl: typeof fetch = fetch,
): Promise<{ per_exercise: ExerciseStats[] }> {
  const offset = -new Date().getTimezoneOffset(); // minutes east of UTC, as the server expects
  const res = await fetchImpl(`${apiUrl.replace(/\/+$/, '')}/api/stats?tz_offset_minutes=${offset}`, {
    headers: authHeaders(accessToken),
  });
  if (!res.ok) throw await apiError(res);
  return (await res.json()) as { per_exercise: ExerciseStats[] };
}

/** One floating fact: a big word and a small caption. `tone` picks its bubble color. */
export type TrendFact = { key: string; big: string; small: string; tone: 'hype' | 'warm' | 'calm' | string };
/** GET /api/trends/{exercise}: facts computed by the server, headline + advice worded by the AI coach (or rules). */
export type TrendWave = { session_id: string; at: string; reps: number; bars: { d: number | null; t: number | null }[] };
export type Trends = {
  exercise: string;
  sets: number;
  facts: TrendFact[];
  headline: string;
  advice: string;
  source: 'agent' | 'rules' | string;
  /** Recent sets' rep waves, oldest first (one bar per rep: d = deepest angle, t = ms). */
  waves?: TrendWave[];
};

/** Your trend for one exercise. `sessionId`: the set just finished (the facts focus on it). Can take ~15 s with the AI. */
export async function getTrends(
  apiUrl: string,
  accessToken: string,
  exercise: string,
  sessionId?: string,
  fetchImpl: typeof fetch = fetch,
): Promise<Trends> {
  const q = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : '';
  const res = await fetchImpl(`${apiUrl.replace(/\/+$/, '')}/api/trends/${encodeURIComponent(exercise)}${q}`, {
    headers: authHeaders(accessToken),
  });
  if (!res.ok) throw await apiError(res);
  return (await res.json()) as Trends;
}

/** Save the phone's rep count for an exercise the server doesn't count (jumping jacks). Call before finishWorkout. */
export async function saveDeviceReps(
  apiUrl: string,
  accessToken: string,
  sessionId: string,
  reps: number,
  fetchImpl: typeof fetch = fetch,
): Promise<void> {
  const res = await fetchImpl(`${apiUrl.replace(/\/+$/, '')}/api/workouts/${sessionId}/device-reps`, {
    method: 'POST',
    headers: authHeaders(accessToken),
    body: JSON.stringify({ reps }),
  });
  if (!res.ok) throw await apiError(res);
}

export type SensorCodeReply = {
  /** e.g. "ABCD-2345": typed (or pasted) into the laptop bridge. */
  code: string;
  expires_in_s: number;
  /** Addresses a bridge on ANOTHER computer can use to reach the server, best first. */
  server_urls: string[];
  /** The exact line to run on the bridge laptop (server address and code filled in). */
  bridge_command: string;
  /** Set when pairing can't work as things stand (e.g. the server only listens on its own computer), with the fix. */
  warning: string | null;
};

/** The bridge command for a code. Prefers the server's own suggestion: the app's API address can be `localhost`,
 * which on the bridge laptop means that laptop, not the server. */
export function bridgeCommand(code: string, apiUrl: string, serverUrls: string[] = []): string {
  const server = serverUrls[0] ?? apiUrl.replace(/\/+$/, '');
  return `python hardware/laptop/spine_bridge.py --server ${server} --code ${code}`;
}

/** A one-time code (e.g. "ABCD-2345") to type into the laptop sensor bridge (hardware/laptop/spine_bridge.py). */
export async function getSensorCode(
  apiUrl: string,
  accessToken: string,
  sessionId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<SensorCodeReply> {
  const res = await fetchImpl(`${apiUrl.replace(/\/+$/, '')}/api/workouts/${sessionId}/sensor-code`, {
    method: 'POST',
    headers: authHeaders(accessToken),
  });
  if (!res.ok) throw await apiError(res);
  const r = (await res.json()) as Partial<SensorCodeReply> & { code: string; expires_in_s: number };
  const serverUrls = Array.isArray(r.server_urls) ? r.server_urls.filter((u) => typeof u === 'string') : [];
  return {
    code: r.code,
    expires_in_s: r.expires_in_s,
    server_urls: serverUrls,
    bridge_command: typeof r.bridge_command === 'string' ? r.bridge_command : bridgeCommand(r.code, apiUrl, serverUrls),
    warning: typeof r.warning === 'string' ? r.warning : null,
  };
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

  /** Ask the wearable on the laptop bridge to recalibrate. The server answers `sensor_not_connected` if none is. */
  sendDeviceCommand(sessionId: string, command: 'recalibrate'): boolean {
    if (!this.isReady) return false;
    this.ws!.send(JSON.stringify({ schema_version: 1, type: 'device_command', session_id: sessionId, command }));
    return true;
  }

  close(): void {
    this.ready = false;
    this.ws?.close();
    this.ws = null;
  }
}

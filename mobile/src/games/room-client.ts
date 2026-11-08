/**
 * Client for live 1v1 Rep Racer rooms: REST to create/join, then one authenticated WebSocket per player.
 * Pure TypeScript over the standard `fetch` / `WebSocket` globals (injectable for tests).
 *
 * Each player's MediaPipe runs in their own browser and only joint ANGLES are sent (never video). The server counts
 * the reps, runs the countdown and the timer, and decides the winner; this file just carries messages and keeps the
 * connection alive. Server times are RELATIVE (ms from now / since the start), so two laptops never need synchronised
 * clocks. See docs/grit-games.md for the message formats.
 */

import type { PoseAngles } from '../pose/angles';
import { PoseStreamer } from '../pose/protocol';
import { FormMonitor, jointAngle, MSG, profileFor, type ExerciseSlug } from './exercise';
import type { FeedbackSource, TrackingState } from './exercise-controller';
import { GamesApiError } from './games-api';

export type RoomStatus = 'lobby' | 'countdown' | 'racing' | 'finished';

export type RoomPlayer = { user_id: string; name: string; ready: boolean; connected: boolean; reps: number; tracking: 'ok' | 'lost'; rematch: boolean };
export type RaceResultLine = { user_id: string; name: string; reps: number; last_rep_ms: number | null; placement: number; result: 'win' | 'loss' | 'draw' | 'forfeit' };

export type RoomState = {
  type: 'room_state';
  room: string;
  you: string;
  race_id: number;
  exercise: ExerciseSlug;
  target_reps: number;
  time_limit_s: number;
  status: RoomStatus;
  host_id: string;
  starts_in_ms: number | null;
  elapsed_ms: number;
  remaining_ms: number | null;
  players: RoomPlayer[];
  winner_id: string | null;
  end_reason: 'target_reached' | 'time_up' | 'forfeit' | null;
  results: RaceResultLine[];
};

export type RoomEvent =
  | RoomState
  | { type: 'race_started'; room: string; race_id: number; time_limit_s: number; target_reps: number }
  | { type: 'rep_counted'; room: string; user_id: string; reps: number; elapsed_ms: number }
  | { type: 'race_finished'; room: string; race_id: number; winner_id: string | null; end_reason: string; elapsed_ms: number; results: RaceResultLine[] }
  | { type: 'coaching_feedback'; room: string; rep_count: number; issue_code: string; message: string; source: string }
  | { type: 'error'; room?: string; code: string; detail: string };

const KNOWN = new Set(['room_state', 'race_started', 'rep_counted', 'race_finished', 'coaching_feedback', 'error']);

export function parseRoomEvent(raw: string): RoomEvent | null {
  try {
    const v: unknown = JSON.parse(raw);
    if (typeof v === 'object' && v !== null && KNOWN.has((v as { type?: string }).type ?? '')) return v as RoomEvent;
  } catch {
    // not JSON
  }
  return null;
}

export type RoomInfo = { code: string; exercise: ExerciseSlug; target_reps: number; time_limit_s: number; status: RoomStatus; host_id: string; players: number };

async function failure(res: Response): Promise<GamesApiError> {
  let detail = '';
  try {
    const d = ((await res.json()) as { detail?: unknown }).detail;
    detail = typeof d === 'string' ? d : typeof d === 'object' && d && 'detail' in d ? String((d as { detail: unknown }).detail) : '';
  } catch {
    // no JSON body
  }
  return new GamesApiError(res.status, detail || `request failed (${res.status})`);
}

const authHeaders = (token: string) => ({ Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' });
const base = (apiUrl: string) => apiUrl.replace(/\/+$/, '');

export async function createRoom(
  apiUrl: string,
  token: string,
  settings: { exercise: ExerciseSlug; targetReps: number; timeLimitS: number; displayName?: string },
  fetchImpl: typeof fetch = fetch,
): Promise<RoomInfo> {
  const res = await fetchImpl(`${base(apiUrl)}/api/games/rooms`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ exercise: settings.exercise, target_reps: settings.targetReps, time_limit_s: settings.timeLimitS, display_name: settings.displayName }),
  });
  if (!res.ok) throw await failure(res);
  return (await res.json()) as RoomInfo;
}

export async function joinRoom(apiUrl: string, token: string, code: string, displayName?: string, fetchImpl: typeof fetch = fetch): Promise<RoomInfo> {
  const res = await fetchImpl(`${base(apiUrl)}/api/games/rooms/${encodeURIComponent(code.trim().toUpperCase())}/join`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ display_name: displayName }),
  });
  if (!res.ok) throw await failure(res);
  return (await res.json()) as RoomInfo;
}

export async function leaveRoomRest(apiUrl: string, token: string, code: string, fetchImpl: typeof fetch = fetch): Promise<void> {
  await fetchImpl(`${base(apiUrl)}/api/games/rooms/${encodeURIComponent(code)}/leave`, { method: 'POST', headers: authHeaders(token) });
}

export type RoomHandlers = {
  onEvent: (event: RoomEvent) => void;
  /** 4401 token rejected/expired, 4404 room gone / not yours, 4410 the seat was taken over by another tab. */
  onClose?: (code: number) => void;
};

/** One connection to a room. The first message is `auth` (the server enforces it). */
export class RoomSocket {
  private ws: WebSocket | null = null;
  private ready = false;
  private readonly apiUrl: string;
  private readonly code: string;
  private readonly token: string;
  private readonly handlers: RoomHandlers;
  private readonly WebSocketImpl: typeof WebSocket;

  constructor(apiUrl: string, code: string, token: string, handlers: RoomHandlers, WebSocketImpl: typeof WebSocket = WebSocket) {
    this.apiUrl = apiUrl;
    this.code = code;
    this.token = token;
    this.handlers = handlers;
    this.WebSocketImpl = WebSocketImpl;
  }

  get isOpen(): boolean {
    return this.ready && this.ws !== null && this.ws.readyState === 1;
  }

  connect(): void {
    const ws = new this.WebSocketImpl(`${base(this.apiUrl).replace(/^http/i, 'ws')}/ws/games/rooms/${encodeURIComponent(this.code)}`);
    this.ws = ws;
    ws.onopen = () => ws.send(JSON.stringify({ schema_version: 1, type: 'auth', access_token: this.token }));
    ws.onmessage = (m: MessageEvent) => {
      const event = typeof m.data === 'string' ? parseRoomEvent(m.data) : null;
      if (!event) return;
      if (event.type === 'room_state') this.ready = true;
      this.handlers.onEvent(event);
    };
    ws.onclose = (e: CloseEvent) => {
      this.ready = false;
      this.handlers.onClose?.(e.code);
    };
  }

  private send(message: object): boolean {
    if (!this.isOpen) return false;
    this.ws!.send(JSON.stringify({ schema_version: 1, ...message }));
    return true;
  }

  sendReady(ready: boolean): boolean {
    return this.send({ type: 'ready', ready });
  }
  sendRematch(): boolean {
    return this.send({ type: 'rematch' });
  }
  sendLeave(): boolean {
    return this.send({ type: 'leave' });
  }
  sendPose(sequence: number, timestampMs: number, data: PoseAngles): boolean {
    return this.send({ type: 'pose_update', sequence, timestamp_ms: timestampMs, data });
  }

  close(): void {
    this.ready = false;
    this.ws?.close();
    this.ws = null;
  }
}

export type RoomSessionHandlers = {
  onState?: (state: RoomState) => void;
  onEvent?: (event: RoomEvent) => void;
  onFeedback?: (message: string, source: FeedbackSource) => void;
  onTracking?: (state: TrackingState) => void;
  onConnection?: (status: 'connecting' | 'open' | 'reconnecting' | 'closed' | 'error', detail?: string) => void;
};

export type RoomSessionOptions = {
  apiUrl: string;
  code: string;
  exercise: ExerciseSlug;
  getToken: () => Promise<string>;
  handlers?: RoomSessionHandlers;
  WebSocketImpl?: typeof WebSocket;
  maxReconnects?: number;
  reconnectDelayMs?: number;
  lostAfterFrames?: number;
};

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/**
 * What a player's laptop runs during a 1v1: it keeps the room socket connected (a dropped connection reconnects to the
 * same seat with a fresh token), streams this player's angles, and reports tracking loss and form hints. Everything
 * competitive (the count, the clock, the winner) arrives from the server in `room_state` / `race_finished`.
 */
export class RoomSession {
  private socket: RoomSocket | null = null;
  private streamer = new PoseStreamer('room');
  // The server's counter needs strictly increasing sequence numbers. They continue across races and rematches, and the
  // start-up clock reading keeps a reloaded page ahead of whatever the server saw from the previous page.
  private readonly sequenceBase = Date.now();
  private readonly form: FormMonitor;
  private lostFrames = 0;
  private tracking: TrackingState = 'idle';
  private closing = false;
  private reconnects = 0;
  state: RoomState | null = null;

  private readonly o: RoomSessionOptions;

  constructor(options: RoomSessionOptions) {
    this.o = options;
    this.form = new FormMonitor(profileFor(options.exercise));
  }

  async connect(): Promise<void> {
    this.o.handlers?.onConnection?.(this.reconnects ? 'reconnecting' : 'connecting');
    const token = await this.o.getToken();
    this.socket = new RoomSocket(
      this.o.apiUrl,
      this.o.code,
      token,
      {
        onEvent: (e) => {
          if (e.type === 'room_state') {
            this.state = e;
            this.reconnects = 0;
            this.o.handlers?.onConnection?.('open');
            this.o.handlers?.onState?.(e);
          } else if (e.type === 'coaching_feedback') {
            this.o.handlers?.onFeedback?.(e.message, 'coach');
          } else if (e.type === 'race_started') {
            this.lostFrames = 0;
            this.form.reset();
          }
          this.o.handlers?.onEvent?.(e);
        },
        onClose: (code) => void this.onClose(code),
      },
      this.o.WebSocketImpl,
    );
    this.socket.connect();
  }

  private async onClose(code: number) {
    if (this.closing) return;
    if (code === 4404 || code === 4410) {
      this.o.handlers?.onConnection?.('error', code === 4404 ? 'The room no longer exists.' : 'This room was opened in another window.');
      return;
    }
    while (!this.closing && this.reconnects < (this.o.maxReconnects ?? 6)) {
      this.reconnects += 1;
      await sleep(this.o.reconnectDelayMs ?? 1000);
      if (this.closing) return;
      try {
        await this.connect();
        return;
      } catch {
        // try again
      }
    }
    if (!this.closing) this.o.handlers?.onConnection?.('error', 'Lost the connection to the room.');
  }

  setReady(ready: boolean) {
    this.socket?.sendReady(ready);
  }
  rematch() {
    this.socket?.sendRematch();
  }

  /** Leave on purpose (button): tell the server, then close. */
  leave() {
    this.closing = true;
    this.socket?.sendLeave();
    this.socket?.close();
    this.socket = null;
    this.o.handlers?.onConnection?.('closed');
  }

  /** Close without telling the server (page closed / navigated away); the seat stays reserved for a short grace period. */
  close() {
    this.closing = true;
    this.socket?.close();
    this.socket = null;
  }

  /** One camera frame from this player. Sent to the server only; nothing is counted here. */
  pushFrame(angles: PoseAngles | null, nowMs = Date.now()): void {
    const joint = profileFor(this.o.exercise).joint;
    const angle = jointAngle(angles, joint);
    if (angles && this.socket) {
      const msg = this.streamer.next(angles);
      if (msg) this.socket.sendPose(this.sequenceBase + msg.sequence, msg.timestamp_ms, msg.data);
    }
    if (angle === null) {
      this.lostFrames += 1;
      if (this.lostFrames >= (this.o.lostAfterFrames ?? 12) && this.tracking !== 'lost') {
        this.tracking = 'lost';
        this.o.handlers?.onTracking?.('lost');
        this.o.handlers?.onFeedback?.(MSG.inView, 'tracking');
      }
      return;
    }
    this.lostFrames = 0;
    if (this.tracking !== 'ok') {
      this.tracking = 'ok';
      this.o.handlers?.onTracking?.('ok');
    }
    const tip = this.form.update(angles, nowMs);
    if (tip) this.o.handlers?.onFeedback?.(tip, 'form');
  }
}

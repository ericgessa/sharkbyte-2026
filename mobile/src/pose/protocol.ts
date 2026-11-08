/**
 * The backend's message formats (docs/event-contracts.md) as TypeScript, plus the frame throttle.
 * Pure: no DOM, no React Native.
 */

import type { PoseAngles } from './angles';

export type PoseUpdate = {
  schema_version: 1;
  type: 'pose_update';
  session_id: string;
  sequence: number;
  timestamp_ms: number;
  data: PoseAngles;
};

export type UserSignal = 'pause' | 'resume' | 'pain';

export type RepMetrics = {
  /** The deepest the counted joint got (knee for squats, elbow for push-ups). Use this one. */
  min_angle_deg: number;
  joint: 'knee' | 'elbow';
  /** The original squat field: same value as `min_angle_deg` for knee exercises, `null` for other joints. */
  min_knee_angle_deg: number | null;
  descent_ms: number;
  ascent_ms: number;
  duration_ms: number;
  max_asymmetry_deg: number | null;
};

/** What the server sends back over the workout WebSocket. `rep_completed` and `coaching_feedback` are the live UI. */
export type ServerEvent =
  | {
      type: 'session_ready';
      session_id: string;
      rep_count: number;
      exercise: string;
      /** false: the workout is logged but the server does NOT count reps or coach this exercise yet. */
      analysis_supported: boolean;
      paused: boolean;
      coaching_enabled: boolean;
    }
  | { type: 'rep_completed'; session_id: string; rep_count: number; metrics: RepMetrics }
  | {
      /** An attempt that did NOT count, and why. `rep_count` is unchanged. Show `message` as-is. */
      type: 'rep_rejected';
      session_id: string;
      rep_count: number;
      reason: 'partial_range' | 'too_fast';
      message: string;
      min_angle_deg: number;
      joint: string;
      duration_ms: number;
    }
  | { type: 'coaching_feedback'; session_id: string; rep_count: number; issue_code: string; message: string; source: 'rules' | 'agent' }
  | { type: 'tracking_status'; session_id: string; tracking: 'lost' | 'recovered' }
  | { type: 'session_state'; session_id: string; paused: boolean; coaching_enabled: boolean }
  | { type: 'error'; code: string; detail: string };

const KNOWN = new Set(['session_ready', 'rep_completed', 'rep_rejected', 'coaching_feedback', 'tracking_status', 'session_state', 'error']);

/** Parse one WebSocket text frame. Returns null for anything that is not a known server event. */
export function parseServerEvent(raw: string): ServerEvent | null {
  try {
    const v: unknown = JSON.parse(raw);
    if (typeof v === 'object' && v !== null && typeof (v as { type?: unknown }).type === 'string' && KNOWN.has((v as { type: string }).type)) {
      return v as ServerEvent;
    }
  } catch {
    // not JSON
  }
  return null;
}

/**
 * Turns frames into `pose_update` messages: numbers them (`sequence` strictly increases, as the server requires),
 * stamps them with the device clock, and caps the rate. About 30 per second is plenty; the server drops floods.
 */
export class PoseStreamer {
  readonly sessionId: string;
  readonly minIntervalMs: number;
  private sequence = 0;
  private lastSentMs = Number.NEGATIVE_INFINITY;
  private readonly now: () => number;

  constructor(sessionId: string, maxFps = 30, now: () => number = Date.now) {
    this.sessionId = sessionId;
    this.minIntervalMs = 1000 / maxFps;
    this.now = now;
  }

  /** The message to send for this frame, or null if it came too soon after the previous one. */
  next(angles: PoseAngles): PoseUpdate | null {
    const t = this.now();
    if (t - this.lastSentMs < this.minIntervalMs) return null;
    this.lastSentMs = t;
    return {
      schema_version: 1,
      type: 'pose_update',
      session_id: this.sessionId,
      sequence: this.sequence++,
      timestamp_ms: Math.round(t),
      data: angles,
    };
  }
}

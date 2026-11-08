/**
 * The spine wearable -> the backend's `sensor_update` message.
 * Pure: no DOM, no React Native. The USB link itself lives in wearable-serial.web.ts.
 *
 * Two kinds of line are understood, so the same page works with either board plugged in:
 *
 * 1. `hardware/firmware/spine_test` (wired, after sending `s`):
 *      {"t":123456,"imu_pitch_deg":30.1,"imu_roll_deg":-2.0,"spine_upper_deg":4.2,"spine_mid_deg":-1.5,"spine_lumbar_deg":18.0}
 * 2. `hardware/firmware/base_station` (the wearable's ESP-NOW link, printed over USB):
 *      {"type":"data","seq":12,"t":4567,"phase":"live","torso":12.3,"upper":1.2,"mid":-0.4,"lumbar":5.6,"sensors":{...},"ok":15,"lost":0}
 *    `torso` is the BNO085 forward tilt (`imu_pitch_deg`); `upper`/`mid`/`lumbar` are the section angles. Lines whose
 *    `phase` is not `live` (the wearable is still calibrating) carry no usable angles and are not sent to the server.
 *
 * `t` is the ESP32's own clock (ms since boot); the server anchors each device's clock separately.
 */

/** Degrees. Every field is optional: a sensor that isn't plugged in is simply left out. */
export type SpineData = {
  /** Upper-back (BNO085) forward tilt vs the calibrated neutral posture, + = forward. */
  imu_pitch_deg?: number;
  /** Upper-back sideways tilt, + = toward the wearer's right. */
  imu_roll_deg?: number;
  /** Bend within each section of the back, + = bending forward. */
  spine_upper_deg?: number;
  spine_mid_deg?: number;
  spine_lumbar_deg?: number;
};

export type SensorUpdate = {
  schema_version: 1;
  type: 'sensor_update';
  session_id: string;
  device_id: string;
  sequence: number;
  timestamp_ms: number;
  data: SpineData;
};

export type WearableSample = { deviceMs: number; data: SpineData };

/** The server's limits (server/app/schemas/sensors.py). Out-of-range values would get the whole message rejected. */
const LIMITS: Record<keyof SpineData, number> = {
  imu_pitch_deg: 360,
  imu_roll_deg: 360,
  spine_upper_deg: 180,
  spine_mid_deg: 180,
  spine_lumbar_deg: 180,
};
const FIELDS = Object.keys(LIMITS) as (keyof SpineData)[];

/**
 * One line from the wearable -> a sample, or null for anything that isn't a usable stream line
 * (calibration prompts, a half-received line, garbage). Unknown keys are dropped; so is any value
 * that is not a finite number within the server's range.
 */
export function parseWearableLine(line: string): WearableSample | null {
  const s = line.trim();
  if (!s.startsWith('{')) return null;
  let v: unknown;
  try {
    v = JSON.parse(s);
  } catch {
    return null;
  }
  if (typeof v !== 'object' || v === null) return null;
  const raw = v as Record<string, unknown>;
  const t = raw.t;
  if (typeof t !== 'number' || !Number.isInteger(t) || t < 0) return null;
  let o = raw;
  if (raw.type === 'data') {
    if (raw.phase !== 'live') return null; // still calibrating: the angles are not relative to a neutral posture yet
    o = { imu_pitch_deg: raw.torso, spine_upper_deg: raw.upper, spine_mid_deg: raw.mid, spine_lumbar_deg: raw.lumbar };
  } else if (raw.type !== undefined) {
    return null; // a status line or anything else
  }
  const data: SpineData = {};
  for (const f of FIELDS) {
    const x = o[f];
    if (typeof x === 'number' && Number.isFinite(x) && Math.abs(x) <= LIMITS[f]) data[f] = x;
  }
  return Object.keys(data).length > 0 ? { deviceMs: t, data } : null;
}

const PHASE_PROMPTS: Record<string, string> = {
  neutral: 'Calibration 1/2: stand tall in neutral posture and hold still...',
  bend_wait: 'Calibration 2/2: slowly bend forward as far as is comfortable and hold...',
  bend_hold: 'Hold it...',
};

/** A short human note for a line that is not a stream sample (calibration prompts, status), or null to ignore it. */
export function describeWearableLine(line: string): string | null {
  const s = line.trim();
  if (!s) return null;
  if (!s.startsWith('{')) return s; // boot messages and prompts from the firmware, shown as they are
  try {
    const v = JSON.parse(s) as { type?: unknown; msg?: unknown; phase?: unknown };
    if (v.type === 'status' && typeof v.msg === 'string') return v.msg;
    if (v.type === 'data' && typeof v.phase === 'string' && v.phase !== 'live') return PHASE_PROMPTS[v.phase] ?? `Wearable: ${v.phase}`;
  } catch {
    // half a line
  }
  return null;
}

/** Serial data arrives in arbitrary chunks; this turns them back into whole lines. */
export class LineSplitter {
  private buf = '';
  private readonly maxLine: number;

  constructor(maxLine = 4096) {
    this.maxLine = maxLine;
  }

  push(chunk: string): string[] {
    this.buf += chunk;
    const parts = this.buf.split('\n');
    this.buf = parts.pop() ?? '';
    if (this.buf.length > this.maxLine) this.buf = ''; // no newline for ages: garbage, don't grow forever
    return parts.map((p) => p.replace(/\r$/, ''));
  }
}

/**
 * Turns wearable samples into `sensor_update` messages for one workout: numbers them and caps the rate
 * (by the device's own clock). The firmware already sends ~20/s; the cap only guards against a flood.
 */
export class SensorStreamer {
  readonly sessionId: string;
  readonly deviceId: string;
  readonly minIntervalMs: number;
  private sequence = 0;
  private lastDeviceMs = Number.NEGATIVE_INFINITY;

  constructor(sessionId: string, deviceId = 'esp32-spine', maxHz = 25) {
    this.sessionId = sessionId;
    this.deviceId = deviceId;
    this.minIntervalMs = 1000 / maxHz;
  }

  next(sample: WearableSample): SensorUpdate | null {
    const gap = sample.deviceMs - this.lastDeviceMs;
    // gap < 0: the ESP32 rebooted and its clock restarted. Accept it; the server re-anchors that device.
    if (gap >= 0 && gap < this.minIntervalMs) return null;
    this.lastDeviceMs = sample.deviceMs;
    return {
      schema_version: 1,
      type: 'sensor_update',
      session_id: this.sessionId,
      device_id: this.deviceId,
      sequence: this.sequence++,
      timestamp_ms: sample.deviceMs,
      data: sample.data,
    };
  }
}

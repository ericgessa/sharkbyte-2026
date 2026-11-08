// Unit tests for the pure pose logic. Run: npm run test:pose   (Node's built-in test runner; no extra packages)
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { test } from 'node:test';

import { computePoseAngles, jointAngleDeg, LM } from './angles.ts';
import { LIBRARY_URL, MEDIAPIPE_VERSION, MODEL_URL, WASM_BASE } from './mediapipe.web.ts';
import { parseServerEvent, PoseStreamer } from './protocol.ts';
import { simulatedPushUpFrames, simulatedSquatFrames } from './simulate.ts';
import { bucketSecondsFor, buildChart, chartWindow, formatSeconds, getReadings, linePath, metricsToFetch, niceDomain, ticks } from './readings.ts';
import { describeWearableLine, LineSplitter, parseWearableLine, SensorStreamer } from './wearable.ts';
import { createWorkout, finishWorkout, getCoachReport, toWsUrl, WorkoutSocket } from './workout-client.ts';
import {
  compactNumber,
  exerciseNoun,
  fastestRace,
  formatDuration,
  getProgress,
  localTzOffsetMinutes,
  shortWeekLabel,
  streakHint,
} from './progress.ts';
import { dayLabel, deleteWorkout, exerciseTitle, getWorkoutDetail, groupByDay, listWorkouts, noteKind, workoutLength } from './history.ts';

const near = (a, b, eps = 0.06) => assert.ok(Math.abs(a - b) <= eps, `${a} is not within ${eps} of ${b}`);

// ---------------------------------------------------------------- joint angle maths

test('a straight limb is 180 degrees, a right angle is 90, a fully folded limb is 0', () => {
  const o = { x: 0, y: 0, z: 0 };
  near(jointAngleDeg({ x: 0, y: 1, z: 0 }, o, { x: 0, y: -1, z: 0 }), 180);
  near(jointAngleDeg({ x: 0, y: 1, z: 0 }, o, { x: 1, y: 0, z: 0 }), 90);
  near(jointAngleDeg({ x: 0, y: 1, z: 0 }, o, { x: 0, y: 2, z: 0 }), 0);
});

test('the angle is measured at the middle point and works in 3D', () => {
  const b = { x: 1, y: 2, z: 3 };
  near(jointAngleDeg({ x: 1, y: 2, z: 4 }, b, { x: 1, y: 3, z: 3 }), 90); // a z-axis ray and a y-axis ray
  const a = { x: 1 + 1, y: 2 + 1, z: 3 };
  const c = { x: 1 + 1, y: 2 - 1, z: 3 };
  near(jointAngleDeg(a, b, c), 90); // a 45 degree pair either side of the x axis
});

test('the angle does not depend on how far the person is from the camera (scale)', () => {
  const a = { x: 0.1, y: 0.9, z: 0.05 };
  const b = { x: 0.2, y: 0.5, z: 0 };
  const c = { x: 0.45, y: 0.1, z: 0.2 };
  const scale = (p, k) => ({ x: p.x * k, y: p.y * k, z: p.z * k });
  near(jointAngleDeg(a, b, c), jointAngleDeg(scale(a, 300), scale(b, 300), scale(c, 300)), 1e-6);
});

test('degenerate or broken input gives null, never NaN or a made-up angle', () => {
  const p = { x: 1, y: 1, z: 1 };
  assert.equal(jointAngleDeg(p, p, { x: 2, y: 2, z: 2 }), null); // a landmark on top of the joint
  assert.equal(jointAngleDeg({ x: NaN, y: 0, z: 0 }, p, { x: 2, y: 2, z: 2 }), null);
  assert.equal(jointAngleDeg({ x: Infinity, y: 0, z: 0 }, p, { x: 2, y: 2, z: 2 }), null);
});

// ---------------------------------------------------------------- frame angles

/** 33 landmarks, all invisible, then set up the legs with chosen knee angles. */
function skeleton({ leftKnee = null, rightKnee = null, visibility = 0.9, leftVis = visibility, rightVis = visibility } = {}) {
  const lm = Array.from({ length: 33 }, () => ({ x: 0, y: 0, z: 0, visibility: 0 }));
  const leg = (hip, knee, ankle, x, deg, vis) => {
    const t = (deg * Math.PI) / 180;
    lm[hip] = { x, y: 1, z: 0, visibility: vis };
    lm[knee] = { x, y: 0, z: 0, visibility: vis };
    lm[ankle] = { x: x + Math.sin(t), y: Math.cos(t), z: 0, visibility: vis }; // angle at the knee = deg
  };
  if (leftKnee !== null) leg(LM.LEFT_HIP, LM.LEFT_KNEE, LM.LEFT_ANKLE, -1, leftKnee, leftVis);
  if (rightKnee !== null) leg(LM.RIGHT_HIP, LM.RIGHT_KNEE, LM.RIGHT_ANKLE, 1, rightKnee, rightVis);
  return lm;
}

test('knee angles come out as built, rounded to one decimal, with the rest null', () => {
  const a = computePoseAngles(skeleton({ leftKnee: 100, rightKnee: 163.37 }));
  near(a.left_knee_angle_deg, 100, 0.06);
  near(a.right_knee_angle_deg, 163.4, 0.06);
  assert.equal(a.left_elbow_angle_deg, null);
  assert.equal(a.right_hip_angle_deg, null);
  assert.equal(Math.round(a.left_knee_angle_deg * 10), a.left_knee_angle_deg * 10); // one decimal place
});

test('a side whose landmarks are not visible enough is reported as null, the other side still counts', () => {
  const a = computePoseAngles(skeleton({ leftKnee: 100, rightKnee: 100, rightVis: 0.3 }));
  near(a.left_knee_angle_deg, 100);
  assert.equal(a.right_knee_angle_deg, null);
});

test('landmark_confidence is the mean visibility of the landmarks actually used', () => {
  const a = computePoseAngles(skeleton({ leftKnee: 120, rightKnee: 120, leftVis: 0.9, rightVis: 0.6 }));
  near(a.landmark_confidence, 0.75, 0.001); // 3 landmarks at 0.9 and 3 at 0.6
  const only = computePoseAngles(skeleton({ leftKnee: 120, rightKnee: 120, leftVis: 0.95, rightVis: 0.2 }));
  near(only.landmark_confidence, 0.95, 0.001); // the unreliable side does not drag the confidence down: it is not reported
});

test('no usable joint at all gives null (the server rejects a frame with no angles)', () => {
  assert.equal(computePoseAngles(skeleton({})), null); // nobody in view
  assert.equal(computePoseAngles(skeleton({ leftKnee: 100, leftVis: 0.1 })), null);
  assert.equal(computePoseAngles([]), null);
});

test('a landmark without a visibility value is treated as not visible, not trusted', () => {
  const lm = skeleton({ leftKnee: 100 }).map(({ x, y, z }) => ({ x, y, z })); // visibility stripped
  assert.equal(computePoseAngles(lm), null);
});

test('visibility can come from a different landmark list than the geometry', () => {
  const world = skeleton({ leftKnee: 100 }).map(({ x, y, z }) => ({ x, y, z }));
  const image = skeleton({ leftKnee: 100, leftVis: 0.8 });
  near(computePoseAngles(world, image).left_knee_angle_deg, 100);
  near(computePoseAngles(world, image).landmark_confidence, 0.8, 0.001);
});

test('elbows and hips are measured too', () => {
  const lm = Array.from({ length: 33 }, () => ({ x: 0, y: 0, z: 0, visibility: 0.9 }));
  lm[LM.LEFT_SHOULDER] = { x: 0, y: 1, z: 0, visibility: 0.9 };
  lm[LM.LEFT_ELBOW] = { x: 0, y: 0, z: 0, visibility: 0.9 };
  lm[LM.LEFT_WRIST] = { x: 1, y: 0, z: 0, visibility: 0.9 }; // 90 degrees at the elbow
  lm[LM.LEFT_HIP] = { x: 0, y: 2, z: 0, visibility: 0.9 };
  lm[LM.LEFT_KNEE] = { x: 0, y: 3, z: 0, visibility: 0.9 }; // shoulder (y=1) - hip (y=2) - knee (y=3): straight
  const a = computePoseAngles(lm);
  near(a.left_elbow_angle_deg, 90);
  near(a.left_hip_angle_deg, 180);
});

// ---------------------------------------------------------------- the pose_update stream

const ANGLES = { left_knee_angle_deg: 90, right_knee_angle_deg: 91, left_elbow_angle_deg: null, right_elbow_angle_deg: null, left_hip_angle_deg: null, right_hip_angle_deg: null, landmark_confidence: 0.9 };

test('messages are numbered from 0, strictly increasing, and shaped as the contract says', () => {
  let t = 1_000_000;
  const s = new PoseStreamer('sid', 30, () => t);
  const sent = [];
  for (let i = 0; i < 4; i++) {
    const u = s.next(ANGLES);
    if (u) sent.push(u);
    t += 40;
  }
  assert.deepEqual(sent.map((u) => u.sequence), [0, 1, 2, 3]);
  assert.deepEqual(sent.map((u) => u.timestamp_ms), [1_000_000, 1_000_040, 1_000_080, 1_000_120]);
  assert.equal(sent[0].schema_version, 1);
  assert.equal(sent[0].type, 'pose_update');
  assert.equal(sent[0].session_id, 'sid');
  assert.deepEqual(sent[0].data, ANGLES);
});

test('frames that arrive faster than the rate cap are dropped without consuming a sequence number', () => {
  let t = 0;
  const s = new PoseStreamer('sid', 30, () => t);
  assert.equal(s.next(ANGLES).sequence, 0);
  t = 10;
  assert.equal(s.next(ANGLES), null); // 10 ms later: too soon for 30 fps
  t = 20;
  assert.equal(s.next(ANGLES), null);
  t = 34;
  assert.equal(s.next(ANGLES).sequence, 1); // no gap in the numbering
});

test('the rate cap is configurable', () => {
  let t = 0;
  const s = new PoseStreamer('sid', 10, () => t);
  s.next(ANGLES);
  t = 99;
  assert.equal(s.next(ANGLES), null);
  t = 100;
  assert.notEqual(s.next(ANGLES), null);
});

// ---------------------------------------------------------------- server events

test('known server events parse; everything else is ignored safely', () => {
  const rep = parseServerEvent(JSON.stringify({ type: 'rep_completed', session_id: 's', rep_count: 2, metrics: { min_knee_angle_deg: 85, min_angle_deg: 85, joint: 'knee' } }));
  assert.equal(rep.type, 'rep_completed');
  assert.equal(rep.rep_count, 2);
  assert.equal(parseServerEvent(JSON.stringify({ type: 'coaching_feedback', message: 'x' })).type, 'coaching_feedback');
  assert.equal(parseServerEvent(JSON.stringify({ type: 'rep_rejected', reason: 'partial_range', message: 'x' })).type, 'rep_rejected');
  assert.equal(parseServerEvent('not json'), null);
  assert.equal(parseServerEvent('"a string"'), null);
  assert.equal(parseServerEvent('[1,2]'), null);
  assert.equal(parseServerEvent('null'), null);
  assert.equal(parseServerEvent(JSON.stringify({ type: 'from_the_future' })), null);
  assert.equal(parseServerEvent(JSON.stringify({ no_type: true })), null);
});

// ---------------------------------------------------------------- REST

test('toWsUrl switches the scheme and tidies the end', () => {
  assert.equal(toWsUrl('http://localhost:8000'), 'ws://localhost:8000');
  assert.equal(toWsUrl('https://api.example.com/'), 'wss://api.example.com');
  assert.equal(toWsUrl('http://192.168.0.5:8000///'), 'ws://192.168.0.5:8000');
  assert.equal(toWsUrl('https://a.example.com/path/http'), 'wss://a.example.com/path/http'); // only the scheme changes
});

function fakeFetch(status, body) {
  const calls = [];
  const impl = async (url, init) => {
    calls.push({ url, init });
    return { ok: status >= 200 && status < 300, status, json: async () => body };
  };
  return { impl, calls };
}

test('createWorkout sends the bearer token and the exercise, and returns the workout', async () => {
  const f = fakeFetch(201, { session_id: 'abc', exercise: 'squat', analysis_supported: true, status: 'active' });
  const w = await createWorkout('http://localhost:8000/', 'TOK', 'push-up', f.impl);
  assert.equal(w.session_id, 'abc');
  const { url, init } = f.calls[0];
  assert.equal(url, 'http://localhost:8000/api/workouts'); // no double slash
  assert.equal(init.method, 'POST');
  assert.equal(init.headers.Authorization, 'Bearer TOK');
  assert.deepEqual(JSON.parse(init.body), { exercise: 'push-up' });
});

test('API errors carry the status and the server\'s explanation', async () => {
  await assert.rejects(createWorkout('http://x', 't', 'squat', fakeFetch(401, { detail: 'missing bearer token: sign in' }).impl), /^Error: 401 missing bearer token: sign in/);
  await assert.rejects(createWorkout('http://x', 't', 'burpee', fakeFetch(422, { detail: [{ msg: 'Unsupported exercise burpee' }] }).impl), /422 Unsupported exercise burpee/);
  const noBody = { ok: false, status: 500, json: async () => { throw new Error('no json'); } };
  await assert.rejects(finishWorkout('http://x', 't', 'sid', async () => noBody), /^Error: 500$/);
});

test('finishWorkout posts to the right path', async () => {
  const f = fakeFetch(200, { total_reps: 5, status: 'completed' });
  assert.equal((await finishWorkout('http://x:8000', 'T', 'sid-1', f.impl)).total_reps, 5);
  assert.equal(f.calls[0].url, 'http://x:8000/api/workouts/sid-1/finish');
});

test('getCoachReport GETs the report with the bearer token and passes errors on', async () => {
  const f = fakeFetch(200, { summary: 'Nice set.', source: 'agent' });
  assert.deepEqual(await getCoachReport('http://x:8000/', 'T', 'sid-1', f.impl), { summary: 'Nice set.', source: 'agent' });
  assert.equal(f.calls[0].url, 'http://x:8000/api/workouts/sid-1/coach-summary');
  assert.equal(f.calls[0].init.method, undefined); // GET
  assert.equal(f.calls[0].init.headers.Authorization, 'Bearer T');
  await assert.rejects(getCoachReport('http://x', 't', 'sid', fakeFetch(429, { detail: 'too many recap requests; slow down' }).impl), /^Error: 429 too many/);
});

// ---------------------------------------------------------------- WebSocket protocol

class FakeWebSocket {
  static last = null;
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.sent = [];
    FakeWebSocket.last = this;
  }
  send(data) { this.sent.push(JSON.parse(data)); }
  close() { this.readyState = 3; this.closed = true; }
  // test controls
  open() { this.readyState = 1; this.onopen?.(); }
  receive(obj) { this.onmessage?.({ data: typeof obj === 'string' ? obj : JSON.stringify(obj) }); }
  drop(code) { this.readyState = 3; this.onclose?.({ code }); }
}

function socket(handlers = {}) {
  const events = [];
  const closes = [];
  const s = new WorkoutSocket('https://api.example.com/', 'sid-9', 'THE-TOKEN', { onEvent: (e) => events.push(e), onClose: (c) => closes.push(c), ...handlers }, FakeWebSocket);
  s.connect();
  return { s, ws: FakeWebSocket.last, events, closes };
}

const update = (n) => ({ schema_version: 1, type: 'pose_update', session_id: 'sid-9', sequence: n, timestamp_ms: n, data: ANGLES });

test('the first message is auth with the token, sent as soon as the socket opens, on the right URL', () => {
  const { ws } = socket();
  assert.equal(ws.url, 'wss://api.example.com/ws/workouts/sid-9');
  assert.deepEqual(ws.sent, []); // nothing before the connection is open
  ws.open();
  assert.deepEqual(ws.sent, [{ schema_version: 1, type: 'auth', access_token: 'THE-TOKEN' }]);
});

test('no frames are sent until the server says session_ready', () => {
  const { s, ws } = socket();
  ws.open();
  assert.equal(s.sendPose(update(0)), false); // authenticated? not yet: dropped
  assert.equal(ws.sent.length, 1); // only the auth message
  ws.receive({ type: 'session_ready', session_id: 'sid-9', rep_count: 0, exercise: 'squat', analysis_supported: true, paused: false, coaching_enabled: true });
  assert.equal(s.isReady, true);
  assert.equal(s.sendPose(update(1)), true);
  assert.deepEqual(ws.sent[1], update(1));
});

test('server events reach the handler, garbage does not', () => {
  const { ws, events } = socket();
  ws.open();
  ws.receive('garbage');
  ws.receive({ type: 'unknown_thing' });
  ws.receive({ type: 'rep_completed', session_id: 'sid-9', rep_count: 1, metrics: {} });
  assert.deepEqual(events.map((e) => e.type), ['rep_completed']);
});

test('pause, resume and pain are sent as user_signal messages for this workout', () => {
  const { s, ws } = socket();
  ws.open();
  assert.equal(s.sendSignal('sid-9', 'pain'), false); // not ready yet
  ws.receive({ type: 'session_ready', session_id: 'sid-9', rep_count: 0, exercise: 'squat', analysis_supported: true, paused: false, coaching_enabled: true });
  assert.equal(s.sendSignal('sid-9', 'pain'), true);
  assert.deepEqual(ws.sent.at(-1), { schema_version: 1, type: 'user_signal', session_id: 'sid-9', signal: 'pain' });
});

test('close codes are passed on and readiness is lost (so frames stop)', () => {
  const { s, ws, closes } = socket();
  ws.open();
  ws.receive({ type: 'session_ready', session_id: 'sid-9', rep_count: 0, exercise: 'squat', analysis_supported: true, paused: false, coaching_enabled: true });
  ws.drop(4401);
  assert.deepEqual(closes, [4401]);
  assert.equal(s.isReady, false);
  assert.equal(s.sendPose(update(2)), false);
});

test('close() shuts the socket and nothing is sent afterwards', () => {
  const { s, ws } = socket();
  ws.open();
  ws.receive({ type: 'session_ready', session_id: 'sid-9', rep_count: 0, exercise: 'squat', analysis_supported: true, paused: false, coaching_enabled: true });
  s.close();
  assert.equal(ws.closed, true);
  assert.equal(s.sendPose(update(3)), false);
});

// ---------------------------------------------------------------- spine wearable (USB serial)

const FULL_LINE = '{"t":123456,"imu_pitch_deg":30.10,"imu_roll_deg":-2.00,"spine_upper_deg":4.20,"spine_mid_deg":-1.50,"spine_lumbar_deg":18.00}';

test('a firmware stream line becomes a sample with the device clock and every spine field', () => {
  assert.deepEqual(parseWearableLine(FULL_LINE + '\r'), {
    deviceMs: 123456,
    data: { imu_pitch_deg: 30.1, imu_roll_deg: -2, spine_upper_deg: 4.2, spine_mid_deg: -1.5, spine_lumbar_deg: 18 },
  });
});

test('a missing sensor is just a missing field', () => {
  assert.deepEqual(parseWearableLine('{"t":5,"spine_lumbar_deg":12.5}'), { deviceMs: 5, data: { spine_lumbar_deg: 12.5 } });
});

test('non-stream lines, garbage and unusable samples are ignored, not forwarded', () => {
  for (const line of [
    '',
    '[Calibration 1/2] Stand tall in your normal posture and hold STILL for 3 seconds...',
    'torso   12.0 | upper    1.0 | mid    0.5 | lumbar    3.0',
    '{"t":12,"spine_lumbar_deg":1', // cut off mid-line
    '{"spine_lumbar_deg":1}', // no device clock
    '{"t":-1,"spine_lumbar_deg":1}',
    '{"t":1.5,"spine_lumbar_deg":1}',
    '{"t":1}', // nothing measured
    '{"t":1,"flex_raw":1780}', // not part of this project
    '[1,2]',
  ]) assert.equal(parseWearableLine(line), null, line);
});

test('values the server would reject are dropped, and unknown keys never reach it', () => {
  const s = parseWearableLine('{"t":1,"spine_lumbar_deg":181,"spine_mid_deg":"3","imu_pitch_deg":20,"surprise":1}');
  assert.deepEqual(s, { deviceMs: 1, data: { imu_pitch_deg: 20 } });
});

test('serial chunks are reassembled into whole lines', () => {
  const sp = new LineSplitter();
  assert.deepEqual(sp.push('{"t":1,"spine_'), []);
  assert.deepEqual(sp.push('mid_deg":2}\r\n{"t":2'), ['{"t":1,"spine_mid_deg":2}']);
  assert.deepEqual(sp.push(',"spine_mid_deg":3}\n\n'), ['{"t":2,"spine_mid_deg":3}', '']);
  const tiny = new LineSplitter(10);
  tiny.push('x'.repeat(50)); // a newline never came: dropped rather than growing forever
  assert.deepEqual(tiny.push('ok\n'), ['ok']);
});

test('samples become numbered sensor_update messages for this workout, on the device clock', () => {
  const st = new SensorStreamer('sid-9');
  const a = st.next({ deviceMs: 1000, data: { spine_lumbar_deg: 5 } });
  const b = st.next({ deviceMs: 1050, data: { spine_lumbar_deg: 6 } });
  assert.deepEqual(a, { schema_version: 1, type: 'sensor_update', session_id: 'sid-9', device_id: 'esp32-spine', sequence: 0, timestamp_ms: 1000, data: { spine_lumbar_deg: 5 } });
  assert.equal(b.sequence, 1);
  assert.equal(b.timestamp_ms, 1050);
});

test('a flood is capped, and an ESP32 reboot (clock going backwards) is not mistaken for one', () => {
  const st = new SensorStreamer('s', 'esp32-spine', 25); // at most one per 40 ms
  assert.ok(st.next({ deviceMs: 1000, data: { spine_mid_deg: 1 } }));
  assert.equal(st.next({ deviceMs: 1010, data: { spine_mid_deg: 1 } }), null);
  assert.ok(st.next({ deviceMs: 1040, data: { spine_mid_deg: 1 } }));
  const afterReboot = st.next({ deviceMs: 30, data: { spine_mid_deg: 1 } });
  assert.ok(afterReboot && afterReboot.timestamp_ms === 30 && afterReboot.sequence === 2);
});

test('sensor updates go over the workout socket only once it is ready', () => {
  const { s, ws } = socket();
  const msg = new SensorStreamer('sid-9').next({ deviceMs: 1, data: { spine_upper_deg: 2 } });
  ws.open();
  assert.equal(s.sendSensor(msg), false);
  ws.receive({ type: 'session_ready', session_id: 'sid-9', rep_count: 0, exercise: 'squat', analysis_supported: true, paused: false, coaching_enabled: true });
  assert.equal(s.sendSensor(msg), true);
  assert.deepEqual(ws.sent.at(-1), msg);
});

// ---------------------------------------------------------------- simulated squats

test('simulated squats match the backend simulator: depths, rest, asymmetry, no unseen joints', () => {
  const frames = simulatedSquatFrames({ depths: [85, 85, 85, 98, 98] });
  assert.equal(frames.length, 10 + 5 * (72 + 10)); // 10 rest + per rep 72 steps + 10 rest (as app/simulation.py)
  assert.equal(frames[0].left_knee_angle_deg, 169);
  assert.equal(frames[0].right_knee_angle_deg, 171); // a 2 degree left/right difference, like the backend
  assert.ok(frames.every((f) => f.left_elbow_angle_deg === null && f.landmark_confidence === 0.95));
  const deepest = (from, to) => Math.min(...frames.slice(from, to).map((f) => f.left_knee_angle_deg));
  near(deepest(10, 92), 84, 0.2); // first rep bottoms out near 85 (left knee reads 1 degree lower)
  near(deepest(10 + 3 * 82, 10 + 5 * 82), 97, 0.2); // the last two reps are shallower: this is what triggers the coach
  assert.ok(frames.every((f) => f.left_knee_angle_deg >= 0 && f.right_knee_angle_deg <= 180));
});

test('simulated push-ups move the elbow angle, not the knee', () => {
  const frames = simulatedPushUpFrames({ depths: [80, 80, 95] });
  assert.ok(frames.every((f) => f.left_knee_angle_deg === null && f.right_knee_angle_deg === null));
  const elbows = frames.map((f) => f.left_elbow_angle_deg);
  assert.ok(elbows.every((v) => v !== null && v >= 0 && v <= 180));
  assert.ok(Math.min(...elbows.slice(0, 70)) < 82 && Math.max(...elbows) > 160); // first rep reaches ~80, tops out near 165
  assert.ok(Math.min(...elbows.slice(-70)) > 93); // the last rep is shallower (95)
});

// ---------------------------------------------------------------- MediaPipe loading configuration

test('the CDN library and WebAssembly version is exactly the version pinned in package.json', () => {
  const pinned = JSON.parse(readFileSync(new URL('../../package.json', import.meta.url), 'utf8')).dependencies['@mediapipe/tasks-vision'];
  assert.equal(pinned, MEDIAPIPE_VERSION, 'package.json must pin the exact version (no ^ or ~) that the CDN URLs use, or loading can break');
  assert.ok(LIBRARY_URL.includes(`@${MEDIAPIPE_VERSION}/vision_bundle.mjs`));
  assert.ok(WASM_BASE.includes(`@${MEDIAPIPE_VERSION}/wasm`));
  assert.ok(MODEL_URL.startsWith('https://storage.googleapis.com/mediapipe-models/pose_landmarker/'));
  // When the package is installed (not in CI, which skips npm install) its types must match the pinned version too.
  const installedFile = new URL('../../node_modules/@mediapipe/tasks-vision/package.json', import.meta.url);
  if (existsSync(installedFile)) assert.equal(JSON.parse(readFileSync(installedFile, 'utf8')).version, MEDIAPIPE_VERSION);
});

// ---------------------------------------------------------------- the wireless base station's lines

const baseLine = (over = {}) =>
  JSON.stringify({ type: 'data', seq: 12, t: 4567, phase: 'live', torso: 12.3, upper: 1.2, mid: -0.4, lumbar: 5.6, sensors: { C: 1.0, B: 6.6, A: 6.2, BNO: 7.4 }, ok: 15, lost: 0, ...over });

test('a base-station "live" line becomes the same sample as a wired stream line', () => {
  const s = parseWearableLine(baseLine());
  assert.deepEqual(s, { deviceMs: 4567, data: { imu_pitch_deg: 12.3, spine_upper_deg: 1.2, spine_mid_deg: -0.4, spine_lumbar_deg: 5.6 } });
});

test('base-station lines are only used once the wearable is calibrated (phase "live")', () => {
  for (const phase of ['neutral', 'bend_wait', 'bend_hold']) assert.equal(parseWearableLine(baseLine({ phase })), null, phase);
});

test('a section that is not available (null) is left out; a line with nothing usable is dropped', () => {
  const s = parseWearableLine(baseLine({ upper: null, mid: null }));
  assert.deepEqual(s.data, { imu_pitch_deg: 12.3, spine_lumbar_deg: 5.6 });
  assert.equal(parseWearableLine(baseLine({ torso: null, upper: null, mid: null, lumbar: null })), null);
});

test('base-station status lines and out-of-range values never become samples', () => {
  assert.equal(parseWearableLine('{"type":"status","msg":"paired with wearable AA:BB"}'), null);
  assert.deepEqual(parseWearableLine(baseLine({ lumbar: 500 })).data.spine_lumbar_deg, undefined); // beyond the server's limit: dropped
});

test('base-station samples stream like any others: numbered, rate-capped, ready for the server', () => {
  const streamer = new SensorStreamer('sess-1');
  const a = streamer.next(parseWearableLine(baseLine({ t: 1000 })));
  assert.deepEqual([a.type, a.device_id, a.sequence, a.timestamp_ms, a.data.spine_lumbar_deg], ['sensor_update', 'esp32-spine', 0, 1000, 5.6]);
  assert.equal(streamer.next(parseWearableLine(baseLine({ t: 1010 }))), null); // faster than 25 Hz
  assert.equal(streamer.next(parseWearableLine(baseLine({ t: 1060 }))).sequence, 1);
});

test('calibration prompts and status lines are described for the screen, samples are not', () => {
  assert.match(describeWearableLine(baseLine({ phase: 'neutral' })), /stand tall/i);
  assert.match(describeWearableLine(baseLine({ phase: 'bend_wait' })), /bend forward/i);
  assert.equal(describeWearableLine('{"type":"status","msg":"paired with wearable"}'), 'paired with wearable');
  assert.equal(describeWearableLine(baseLine()), null);
  assert.equal(describeWearableLine('=== Spine test ==='), '=== Spine test ===');
  assert.equal(describeWearableLine('  '), null);
});

// ---------------------------------------------------------------- progress (GET /api/stats)

test('getProgress sends the token, the time zone and the week count', async () => {
  const calls = [];
  const impl = async (url, init) => {
    calls.push({ url, init });
    return { ok: true, status: 200, json: async () => ({ streak: { current_days: 3, longest_days: 5 } }) };
  };
  const p = await getProgress('http://x:8000/', 'T', { weeks: 8, tzOffsetMinutes: -300 }, impl);
  assert.equal(p.streak.current_days, 3);
  assert.equal(calls[0].url, 'http://x:8000/api/stats?tz_offset_minutes=-300&weeks=8');
  assert.equal(calls[0].init.headers.Authorization, 'Bearer T');
  await assert.rejects(getProgress('http://x', 't', {}, async () => ({ ok: false, status: 401, json: async () => ({}) })), /401/);
});

test('the time zone offset is minutes EAST of UTC (the opposite of getTimezoneOffset)', () => {
  assert.equal(localTzOffsetMinutes({ getTimezoneOffset: () => 300 }), -300); // US Eastern
  assert.equal(localTzOffsetMinutes({ getTimezoneOffset: () => -60 }), 60);
});

test('numbers and durations are compact', () => {
  assert.equal(compactNumber(0), '0');
  assert.equal(compactNumber(1284), '1,284');
  assert.equal(compactNumber(12_900), '12.9K');
  assert.equal(compactNumber(250_000), '250K');
  assert.equal(compactNumber(1_200_000), '1.2M');
  assert.equal(formatDuration(0), '0m');
  assert.equal(formatDuration(2700), '45m');
  assert.equal(formatDuration(12_000), '3h 20m');
  assert.equal(formatDuration(7200), '2h');
});

test('exercise names pluralise sensibly', () => {
  assert.equal(exerciseNoun('squat', 24), 'squats');
  assert.equal(exerciseNoun('push-up', 1), 'push-up');
  assert.equal(exerciseNoun('push-up', 10), 'push-ups');
});

test('the streak hint says what to do today', () => {
  const now = new Date(2026, 10, 8, 18, 0); // Nov 8, local time
  const today = new Date(2026, 10, 8, 9, 0).toISOString();
  const yesterday = new Date(2026, 10, 7, 9, 0).toISOString();
  const p = (current, longest, workouts, last) => ({ streak: { current_days: current, longest_days: longest }, totals: { workouts, last_workout_at: last } });
  assert.match(streakHint(p(0, 0, 0, null), now), /first workout/);
  assert.match(streakHint(p(3, 5, 9, today), now), /Done for today. See you tomorrow/);
  assert.match(streakHint(p(5, 5, 9, today), now), /best streak yet/);
  assert.match(streakHint(p(3, 5, 9, yesterday), now), /keep it going/);
  assert.match(streakHint(p(0, 4, 9, '2026-10-01T12:00:00Z'), now), /best was 4 days/);
});

test('week labels do not shift with the time zone', () => {
  assert.equal(shortWeekLabel('2026-11-02'), 'Nov 2');
});

test('the fastest Rep Racer finish is picked and worded', () => {
  assert.equal(fastestRace(null), null);
  assert.equal(fastestRace({ races: 0, finished: 0, fastest: [] }), null);
  const games = { races: 3, finished: 2, fastest: [
    { exercise: 'squat', target_reps: 10, duration_ms: 31_000, mode: 'solo', achieved_at: null },
    { exercise: 'push-up', target_reps: 10, duration_ms: 24_340, mode: 'solo', achieved_at: null },
  ] };
  assert.equal(fastestRace(games), '10 push-ups in 24.3 s');
});

// ---------------------------------------------------------------- workout history

test('history calls hit the right endpoints with the token', async () => {
  const calls = [];
  const ok = (body) => async (url, init) => {
    calls.push({ url, init });
    return { ok: true, status: 200, json: async () => body };
  };
  await listWorkouts('http://x:8000/', 'T', 50, ok([]));
  await getWorkoutDetail('http://x:8000', 'T', 'sid-1', ok({}));
  await deleteWorkout('http://x:8000', 'T', 'sid-1', ok(null));
  assert.equal(calls[0].url, 'http://x:8000/api/workouts?limit=50');
  assert.equal(calls[1].url, 'http://x:8000/api/workouts/sid-1/summary');
  assert.equal(calls[2].url, 'http://x:8000/api/workouts/sid-1');
  assert.equal(calls[2].init.method, 'DELETE');
  assert.ok(calls.every((c) => c.init.headers.Authorization === 'Bearer T'));
});

test('a failed delete keeps the server\'s reason (503 = nothing was deleted, try again)', async () => {
  const res = { ok: false, status: 503, json: async () => ({ detail: 'could not delete the sensor data right now' }) };
  await assert.rejects(deleteWorkout('http://x', 't', 'sid', async () => res), /^Error: 503 could not delete/);
});

test('days read Today / Yesterday / a date, in the user\'s own time zone', () => {
  const now = new Date(2026, 10, 8, 20, 0);
  assert.equal(dayLabel(new Date(2026, 10, 8, 7, 0).toISOString(), now), 'Today');
  assert.equal(dayLabel(new Date(2026, 10, 7, 23, 30).toISOString(), now), 'Yesterday');
  assert.equal(dayLabel(new Date(2026, 10, 3, 12, 0).toISOString(), now), 'Tue, Nov 3');
  assert.match(dayLabel(new Date(2025, 11, 30, 12, 0).toISOString(), now), /2025/);
});

test('the list is grouped by day, newest first, order kept', () => {
  const now = new Date(2026, 10, 8, 20, 0);
  const at = (d, h) => ({ started_at: new Date(2026, 10, d, h).toISOString(), id: `${d}-${h}` });
  const groups = groupByDay([at(8, 18), at(8, 9), at(7, 12), at(3, 10)], now);
  assert.deepEqual(groups.map((g) => g.day), ['Today', 'Yesterday', 'Tue, Nov 3']);
  assert.deepEqual(groups[0].items.map((i) => i.id), ['8-18', '8-9']);
});

test('workout length and names read naturally', () => {
  assert.equal(workoutLength('2026-11-08T10:00:00Z', null), null);
  assert.equal(workoutLength('2026-11-08T10:00:00Z', '2026-11-08T10:00:45Z'), '45 s');
  assert.equal(workoutLength('2026-11-08T10:00:00Z', '2026-11-08T10:12:10Z'), '12 min');
  assert.equal(workoutLength('2026-11-08T10:00:00Z', '2026-11-08T11:05:00Z'), '1 h 5 min');
  assert.equal(exerciseTitle('push-up'), 'Push-up');
  assert.equal(noteKind('rep_rejected'), 'not counted');
  assert.equal(noteKind('recommend_rest'), 'rest');
  assert.equal(noteKind('send_feedback'), 'cue');
});

// ---------------------------------------------------------------- "angle over time" from the stored readings (Tiger Data)

const T0 = '2026-11-08T12:00:00.000Z';
const at = (sec) => new Date(Date.parse(T0) + sec * 1000).toISOString();
const row = (sec, metric, value, extra = {}) => ({ time: at(sec), source: 'camera', sensor_id: '', metric, value, samples: 3, ...extra });

test('the chart window runs from the workout start to its end, and an unfinished or broken one has none', () => {
  const w = chartWindow(T0, at(60));
  assert.equal(w.durationS, 60);
  assert.ok(Date.parse(w.start) < Date.parse(T0) && Date.parse(w.end) > Date.parse(at(60)));
  assert.equal(chartWindow(T0, null), null);
  assert.equal(chartWindow(T0, at(-5)), null);
  assert.equal(chartWindow('nope', at(5)), null);
});

test('the bucket size keeps about 240 points per line however long the workout is', () => {
  assert.equal(bucketSecondsFor(24), 0.1);
  assert.equal(bucketSecondsFor(120), 0.5);
  assert.equal(bucketSecondsFor(2400), 10);
  assert.equal(bucketSecondsFor(1), 0.1);
});

test('the metrics asked for are the counted joint on both sides plus the three back sections', () => {
  assert.deepEqual(metricsToFetch('knee'), ['left_knee_angle', 'right_knee_angle', 'spine_lumbar', 'spine_mid', 'spine_upper']);
  assert.deepEqual(metricsToFetch('elbow').slice(0, 2), ['left_elbow_angle', 'right_elbow_angle']);
});

test('the left and right sides become one line, in time order, in seconds since the start', () => {
  const rows = [row(2, 'right_knee_angle', 100), row(1, 'left_knee_angle', 170), row(1, 'right_knee_angle', 160), row(2, 'left_knee_angle', 90)];
  const c = buildChart(rows, T0, at(10), 'knee');
  assert.deepEqual(c.angle.points, [{ t: 1, v: 165 }, { t: 2, v: 95 }]);
  assert.equal(c.angle.label, 'Knee angle');
  assert.equal(c.durationS, 10);
  assert.deepEqual(c.back, []);
});

test('a push-up chart uses the elbow and ignores knee data', () => {
  const rows = [row(1, 'left_knee_angle', 100), row(1, 'left_elbow_angle', 150), row(1, 'right_elbow_angle', 154)];
  const c = buildChart(rows, T0, at(5), 'elbow');
  assert.deepEqual(c.angle.points, [{ t: 1, v: 152 }]);
  assert.equal(c.angle.label, 'Elbow angle');
});

test('wearable back sections are separate lines, lower back first, and only the ones that have data', () => {
  const rows = [row(1, 'spine_upper', 2, { source: 'imu' }), row(2, 'spine_lumbar', 20, { source: 'imu' }), row(1, 'spine_lumbar', 5, { source: 'imu' }), row(1, 'imu_pitch', 30, { source: 'imu' })];
  const c = buildChart(rows, T0, at(5), 'knee');
  assert.deepEqual(c.back.map((s) => s.label), ['Lower back', 'Upper back']);
  assert.deepEqual(c.back[0].points, [{ t: 1, v: 5 }, { t: 2, v: 20 }]); // sorted by time
  assert.equal(c.angle, null); // no camera data in this workout
});

test('each saved rep becomes a band on the chart', () => {
  const reps = [
    { rep_number: 1, metrics: { started_at: at(1), ended_at: at(3) } },
    { rep_number: 2, metrics: { started_at: at(4), ended_at: at(6.5) } },
    { rep_number: 3, metrics: {} }, // an older rep with no recorded window: no band
  ];
  const c = buildChart([row(1, 'left_knee_angle', 150)], T0, at(10), 'knee', reps);
  assert.deepEqual(c.repBands, [{ rep: 1, t0: 1, t1: 3 }, { rep: 2, t0: 4, t1: 6.5 }]);
});

test('odd rows never crash the chart: NaN values, bad times, unknown metrics', () => {
  const rows = [row(1, 'left_knee_angle', NaN), { ...row(1, 'left_knee_angle', 100), time: 'garbage' }, row(1, 'something_else', 5), row(2, 'left_knee_angle', 120)];
  const c = buildChart(rows, T0, at(5), 'knee');
  assert.deepEqual(c.angle.points, [{ t: 2, v: 120 }]);
  assert.equal(buildChart([], T0, at(5), 'knee').angle, null);
});

test('the reading count is the stored samples behind the averaged points', () => {
  const rows = [row(1, 'left_knee_angle', 100, { samples: 7 }), row(1, 'spine_lumbar', 5, { samples: 5, source: 'imu' })];
  assert.equal(buildChart(rows, T0, at(5), 'knee').readings, 12);
});

test('axis ranges are padded and rounded to tidy numbers', () => {
  assert.deepEqual(niceDomain([88, 171]), [80, 180]);
  assert.deepEqual(niceDomain([5, 5.5]), [0, 20]); // never a zero-height axis
  assert.deepEqual(niceDomain([]), [0, 180]);
  assert.deepEqual(ticks([80, 180], 4), [80, 105, 130, 155, 180]);
});

test('a line is drawn in the plot box with larger values higher up, and a long gap breaks it', () => {
  const joined = linePath([{ t: 0, v: 0 }, { t: 1, v: 100 }, { t: 2, v: 50 }], [0, 2], [0, 100], 200, 100);
  assert.equal(joined, 'M0.0 100.0 L100.0 0.0 L200.0 50.0');
  const gappy = linePath([{ t: 0, v: 0 }, { t: 5, v: 100 }, { t: 10, v: 50 }], [0, 10], [0, 100], 200, 100); // 5 s gaps (> 1.5 s)
  assert.equal(gappy, 'M0.0 100.0 M100.0 0.0 M200.0 50.0');
  assert.equal(linePath([], [0, 1], [0, 1], 10, 10), '');
  assert.equal(linePath([{ t: 50, v: 1 }], [0, 10], [0, 10], 10, 10), ''); // outside the axis: not drawn
});

test('formatSeconds reads naturally', () => {
  assert.deepEqual([formatSeconds(4.4), formatSeconds(59.6), formatSeconds(75), formatSeconds(605)], ['4s', '60s', '1:15', '10:05']);
});

test('readings are requested downsampled, for one workout, with one metric parameter per series', async () => {
  const calls = [];
  const f = async (url, init) => (calls.push({ url, init }), { ok: true, status: 200, json: async () => ({ available: true, readings: [] }) });
  const out = await getReadings('http://api.test/', 'tok', 'w1', { start: '2026-11-08T11:59:59Z', end: '2026-11-08T12:10:00Z', metrics: ['left_knee_angle', 'spine_lumbar'], bucketSeconds: 0.5 }, f);
  assert.deepEqual(out, { available: true, readings: [] });
  const u = new URL(calls[0].url);
  assert.equal(u.pathname, '/api/workouts/w1/readings');
  assert.deepEqual(u.searchParams.getAll('metric'), ['left_knee_angle', 'spine_lumbar']);
  assert.equal(u.searchParams.get('bucket_seconds'), '0.5');
  assert.equal(u.searchParams.get('limit'), '5000');
  assert.equal(calls[0].init.headers.Authorization, 'Bearer tok');
});

test('a failed readings request says why, and "not available" is a normal answer, not an error', async () => {
  const bad = async () => ({ ok: false, status: 404, json: async () => ({ detail: 'session not found' }) });
  await assert.rejects(getReadings('http://api.test', 't', 'w', { start: 'a', end: 'b', metrics: [], bucketSeconds: 1 }, bad), /404 session not found/);
  const off = async () => ({ ok: true, status: 200, json: async () => ({ available: false, reason: 'not_configured', readings: [] }) });
  assert.deepEqual(await getReadings('http://api.test', 't', 'w', { start: 'a', end: 'b', metrics: [], bucketSeconds: 1 }, off), { available: false, reason: 'not_configured', readings: [] });
});

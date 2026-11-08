// Unit tests for GRIT Games' (Rep Racer) pure logic and its network clients. Run: npm run test:games (Node's built-in test runner).
// Everything is simulated: fake WebSockets, fake fetch, synthetic joint angles. No camera, no server, no database.
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { test } from 'node:test';

import { ExerciseController } from './exercise-controller.ts';
import { AdaptiveTop, bodyPosition, FormMonitor, jointAngle, MSG, PROFILES, repFeedback } from './exercise.ts';
import { GamesApiError, getBest, saveGameResult } from './games-api.ts';
import { describeComparison, ghostAt, parseSetting, SoloRace } from './racer-engine.ts';
import { createRoom, joinRoom, parseRoomEvent, RoomSession } from './room-client.ts';

const near = (a, b, eps = 1e-6) => assert.ok(Math.abs(a - b) <= eps, `${a} is not within ${eps} of ${b}`);
const frame = (knee, extra = {}) => ({
  left_knee_angle_deg: knee,
  right_knee_angle_deg: knee,
  left_elbow_angle_deg: null,
  right_elbow_angle_deg: null,
  left_hip_angle_deg: null,
  right_hip_angle_deg: null,
  landmark_confidence: 0.95,
  ...extra,
});
const elbow = (deg, hip = null) => frame(null, { left_knee_angle_deg: null, right_knee_angle_deg: null, left_elbow_angle_deg: deg, right_elbow_angle_deg: deg, left_hip_angle_deg: hip, right_hip_angle_deg: hip });

// ------------------------------------------------------------------ exercise: position, feedback, form hints

test('standing is the top of the movement and a full squat is the bottom', () => {
  const p = PROFILES.squat;
  near(bodyPosition(170, p.bottomDeg, p.topDeg), 0);
  near(bodyPosition(p.topDeg, p.bottomDeg, p.topDeg), 0);
  near(bodyPosition(p.bottomDeg, p.bottomDeg, p.topDeg), 1);
  near(bodyPosition(60, p.bottomDeg, p.topDeg), 1); // deeper than the range: clamped
  const mid = bodyPosition((p.topDeg + p.bottomDeg) / 2, p.bottomDeg, p.topDeg);
  near(mid, 0.5);
});

test('the counted joint is the knee for squats and the elbow for push-ups, averaged over the visible sides', () => {
  assert.equal(jointAngle(null, 'knee'), null);
  assert.equal(jointAngle(frame(null, { left_knee_angle_deg: null, right_knee_angle_deg: null }), 'knee'), null);
  near(jointAngle(frame(null, { left_knee_angle_deg: 100, right_knee_angle_deg: 110 }), 'knee'), 105);
  near(jointAngle(frame(null, { left_knee_angle_deg: 100, right_knee_angle_deg: null }), 'knee'), 100);
  assert.equal(jointAngle(frame(120), 'elbow'), null); // knee data is not elbow data
  near(jointAngle(elbow(90), 'elbow'), 90);
});

test('a person whose standing angle reads low can still reach the top of the movement', () => {
  const top = new AdaptiveTop(PROFILES.squat);
  let t = 0;
  let last = 0;
  for (let i = 0; i < 100; i++) last = top.update(152, (t += 33)); // always stands at 152 degrees
  assert.ok(last <= 152 && last >= PROFILES.squat.topFloorDeg, `top is ${last}`);
  near(bodyPosition(152, PROFILES.squat.bottomDeg, last), 0, 0.03);
  const fresh = new AdaptiveTop(PROFILES.squat);
  assert.equal(fresh.top, PROFILES.squat.topDeg); // nothing seen yet: the default
});

test('the top does not creep down while a squat is held', () => {
  const top = new AdaptiveTop(PROFILES.squat);
  let t = 0;
  for (let i = 0; i < 30; i++) top.update(170, (t += 33));
  const before = top.top;
  for (let i = 0; i < 90; i++) top.update(100, (t += 33)); // 3 s at the bottom
  assert.equal(top.top, before);
});

test('rep feedback is positive for a deep rep and asks for depth otherwise', () => {
  assert.equal(repFeedback(PROFILES.squat, 90), 'Good rep!');
  assert.equal(repFeedback(PROFILES.squat, 109), 'Squat deeper.');
  assert.equal(repFeedback(PROFILES['push-up'], 109), 'Lower your chest further.');
});

function descend(monitor, from, to, steps, t0) {
  const tips = [];
  for (let i = 0; i <= steps; i++) {
    const tip = monitor.update(frame(from + ((to - from) * i) / steps), t0 + i * 33);
    if (tip) tips.push(tip);
  }
  return tips;
}

test('"Stand fully before the next rep." fires when you go down again from a half-stand', () => {
  const m = new FormMonitor(PROFILES.squat);
  assert.deepEqual(descend(m, 170, 95, 20, 0), []); // a normal descent
  assert.deepEqual(descend(m, 95, 130, 10, 1000), []); // rising, only part of the way
  assert.deepEqual(descend(m, 130, 100, 10, 2000), [MSG.standFully]);
});

test('standing fully first means no hint', () => {
  const m = new FormMonitor(PROFILES.squat);
  const all = [...descend(m, 170, 95, 20, 0), ...descend(m, 95, 168, 20, 1000), ...descend(m, 168, 95, 20, 3000)];
  assert.deepEqual(all, []);
});

test('hints are rate limited so the screen does not flicker', () => {
  const m = new FormMonitor(PROFILES.squat, 3000);
  descend(m, 170, 95, 10, 0);
  descend(m, 95, 130, 5, 500);
  assert.deepEqual(descend(m, 130, 100, 5, 700), [MSG.standFully]);
  descend(m, 100, 130, 5, 900);
  assert.deepEqual(descend(m, 130, 100, 5, 1100), []); // inside the 3 s cooldown
});

test('push-ups get a back-alignment hint when the hips sag; squats never do (one camera cannot judge a spine)', () => {
  const pu = new FormMonitor(PROFILES['push-up']);
  assert.equal(pu.update(elbow(120, 175), 0), null); // straight body
  assert.equal(pu.update(elbow(120, 120), 5000), MSG.backAligned); // sagging
  const sq = new FormMonitor(PROFILES.squat);
  assert.equal(sq.update(frame(120, { left_hip_angle_deg: 90, right_hip_angle_deg: 90 }), 0), null);
});

// ------------------------------------------------------------------ Rep Racer: solo goal, timer, ghost

test('reaching the target in time wins', () => {
  const r = new SoloRace(3, 30_000);
  r.start();
  assert.equal(r.onRep(4000), true);
  assert.equal(r.onRep(9000), true);
  near(r.progress, 2 / 3);
  assert.equal(r.status, 'running');
  assert.equal(r.onRep(15_000), true);
  assert.equal(r.status, 'won');
  assert.equal(r.finishMs, 15_000);
  assert.equal(r.progress, 1);
  assert.deepEqual(r.repTimesMs, [4000, 9000, 15_000]);
  assert.equal(r.onRep(16_000), false, 'nothing counts after the finish');
  assert.equal(r.reps, 3);
});

test('running out of time ends the race and later reps are ignored', () => {
  const r = new SoloRace(10, 20_000);
  r.start();
  r.onRep(5000);
  r.tick(19_999);
  assert.equal(r.status, 'running');
  assert.equal(r.remainingMs, 1);
  r.tick(20_001);
  assert.equal(r.status, 'expired');
  assert.equal(r.elapsedMs, 20_000);
  assert.equal(r.remainingMs, 0);
  assert.equal(r.onRep(20_500), false);
  assert.equal(r.reps, 1);
  assert.equal(r.finishMs, null);
});

test('a rep that arrives after the buzzer does not sneak in', () => {
  const r = new SoloRace(2, 10_000);
  r.start();
  r.onRep(9000);
  assert.equal(r.onRep(10_400), false);
  assert.equal(r.status, 'expired');
});

test('a race restarts cleanly', () => {
  const r = new SoloRace(1, 5000);
  r.start();
  r.onRep(1000);
  assert.equal(r.status, 'won');
  r.start();
  assert.deepEqual([r.status, r.reps, r.repTimesMs, r.elapsedMs, r.finishMs], ['running', 0, [], 0, null]);
});

test('the clock never runs backwards', () => {
  const r = new SoloRace(5, 60_000);
  r.start();
  r.tick(5000);
  r.tick(3000);
  assert.equal(r.elapsedMs, 5000);
});

test('the ghost replays recorded rep times and nothing else', () => {
  const rec = [3000, 6000, 9000];
  assert.deepEqual(ghostAt(rec, 0, 3), { reps: 0, progress: 0, finished: false });
  const mid = ghostAt(rec, 1500, 3); // half way to its first rep
  assert.equal(mid.reps, 0);
  near(mid.progress, 0.5 / 3);
  assert.equal(ghostAt(rec, 3000, 3).reps, 1);
  assert.equal(ghostAt(rec, 8999, 3).reps, 2);
  const done = ghostAt(rec, 9000, 3);
  assert.deepEqual([done.reps, done.progress, done.finished], [3, 1, true]);
  assert.equal(ghostAt(rec, 99_000, 3).progress, 1);
});

test('a ghost from an unfinished attempt stops where that attempt stopped', () => {
  const g = ghostAt([4000, 9000], 60_000, 10);
  assert.deepEqual([g.reps, g.progress], [2, 0.2]);
  assert.equal(ghostAt([], 5000, 10).progress, 0);
});

test('a ghost from a longer race is cut at this race\'s target', () => {
  assert.equal(ghostAt([1000, 2000, 3000, 4000], 99_000, 2).reps, 2);
});

test('personal best comparison: first time, faster, slower, and incomplete attempts', () => {
  const now = { completed: true, durationMs: 14_000, reps: 5 };
  assert.equal(describeComparison(now, null).kind, 'first');
  assert.match(describeComparison(now, { completed: true, durationMs: 15_500, reps: 5 }).text, /1\.5 s faster/);
  assert.equal(describeComparison(now, { completed: true, durationMs: 13_000, reps: 5 }).kind, 'slower');
  assert.equal(describeComparison(now, { completed: true, durationMs: 14_000, reps: 5 }).kind, 'same');
  assert.equal(describeComparison({ completed: false, durationMs: 30_000, reps: 4 }, { completed: false, durationMs: 30_000, reps: 3 }).kind, 'more_reps');
  assert.equal(describeComparison({ completed: false, durationMs: 30_000, reps: 2 }, { completed: true, durationMs: 15_000, reps: 5 }).kind, 'fewer_reps');
});

test('custom targets and time limits are clamped to what the server accepts', () => {
  assert.equal(parseSetting('12', 1, 100), 12);
  assert.equal(parseSetting('0', 1, 100), 1);
  assert.equal(parseSetting('500', 1, 100), 100);
  assert.equal(parseSetting('abc', 1, 100), null);
  assert.equal(parseSetting('', 1, 100), null);
  assert.equal(parseSetting('-4', 1, 100), null);
  assert.equal(parseSetting('7.5', 1, 100), null);
});

// ------------------------------------------------------------------ the shared controller (fake network)

class FakeWebSocket {
  static instances = [];
  constructor(url) {
    this.url = url;
    this.sent = [];
    this.readyState = 0;
    FakeWebSocket.instances.push(this);
    queueMicrotask(() => {
      this.readyState = 1;
      this.onopen?.();
    });
  }
  send(text) {
    this.sent.push(JSON.parse(text));
  }
  close() {
    this.readyState = 3;
    this.onclose?.({ code: 1000 });
  }
  serverSends(obj) {
    this.onmessage?.({ data: JSON.stringify(obj) });
  }
  serverCloses(code) {
    this.readyState = 3;
    this.onclose?.({ code });
  }
  get poses() {
    return this.sent.filter((m) => m.type === 'pose_update');
  }
}
const lastSocket = () => FakeWebSocket.instances.at(-1);
const wait = (ms = 0) => new Promise((r) => setTimeout(r, ms));

function fakeFetch(routes) {
  const calls = [];
  const impl = async (url, init = {}) => {
    calls.push({ url, method: init.method ?? 'GET', body: init.body ? JSON.parse(init.body) : null, headers: init.headers });
    const key = `${init.method ?? 'GET'} ${new URL(url).pathname}`;
    const handler = routes[key] ?? Object.entries(routes).find(([k]) => key.startsWith(k.replace(/\*$/, '')) && k.endsWith('*'))?.[1];
    const r = typeof handler === 'function' ? handler(init) : handler ?? { status: 404, body: { detail: 'nope' } };
    return { ok: (r.status ?? 200) < 400, status: r.status ?? 200, json: async () => r.body };
  };
  impl.calls = calls;
  return impl;
}

async function startController(overrides = {}) {
  FakeWebSocket.instances.length = 0;
  const log = { reps: [], feedback: [], tracking: [], status: [] };
  const fetchImpl = fakeFetch({
    'POST /api/workouts': { status: 201, body: { session_id: 'sess-1', exercise: 'squat', analysis_supported: true, status: 'active' } },
    'POST /api/workouts/sess-1/finish': { body: { total_reps: 3, status: 'completed' } },
    ...(overrides.routes ?? {}),
  });
  let now = 1_700_000_000_000;
  const controller = new ExerciseController({
    exercise: overrides.exercise ?? 'squat',
    apiUrl: 'http://api.test',
    getToken: async () => 'tok',
    handlers: {
      onRep: (e) => log.reps.push(e),
      onFeedback: (m, s) => log.feedback.push([m, s]),
      onTracking: (t) => log.tracking.push(t),
      onStatus: (s) => log.status.push(s),
    },
    now: () => (now += 34),
    fetchImpl,
    WebSocketImpl: FakeWebSocket,
    reconnectDelayMs: 0,
    ...overrides.options,
  });
  const started = controller.start();
  await wait();
  lastSocket().serverSends({ type: 'session_ready', session_id: 'sess-1', rep_count: 0, exercise: overrides.exercise ?? 'squat', analysis_supported: true, paused: false, coaching_enabled: true });
  await started;
  return { controller, log, fetchImpl, socket: lastSocket() };
}

const repEvent = (n, min = 88) => ({ type: 'rep_completed', session_id: 'sess-1', rep_count: n, metrics: { min_angle_deg: min, joint: 'knee', min_knee_angle_deg: min, descent_ms: 500, ascent_ms: 500, duration_ms: 1000, max_asymmetry_deg: 2 } });

test('starting creates a workout for the chosen exercise, authenticates, and is ready when the server says so', async () => {
  const { controller, fetchImpl, socket, log } = await startController();
  assert.equal(fetchImpl.calls[0].url, 'http://api.test/api/workouts');
  assert.deepEqual(fetchImpl.calls[0].body, { exercise: 'squat' });
  assert.equal(fetchImpl.calls[0].headers.Authorization, 'Bearer tok');
  assert.deepEqual(socket.sent[0], { schema_version: 1, type: 'auth', access_token: 'tok' });
  assert.equal(controller.status, 'ready');
  assert.equal(controller.sessionId, 'sess-1');
  assert.deepEqual(log.status, ['connecting', 'ready']);
});

test('each server-counted rep becomes exactly one event, and a repeated message is ignored', async () => {
  const { controller, log, socket } = await startController();
  socket.serverSends(repEvent(1));
  socket.serverSends(repEvent(1)); // duplicate delivery
  socket.serverSends(repEvent(2, 90));
  socket.serverSends(repEvent(1)); // late, out of order
  assert.deepEqual(log.reps.map((r) => r.repNumber), [1, 2]);
  assert.ok(log.reps.every((r) => r.exercise === 'squat' && r.isValid === true && typeof r.timestamp === 'number'));
  assert.equal(controller.repCount, 2);
});

test('rep feedback comes from how deep the rep was', async () => {
  const { log, socket } = await startController();
  socket.serverSends(repEvent(1, 85));
  socket.serverSends(repEvent(2, 108));
  assert.deepEqual(log.reps.map((r) => r.formFeedback), ['Good rep!', 'Squat deeper.']);
  assert.deepEqual(log.feedback.filter(([, s]) => s === 'rep').map(([m]) => m), ['Good rep!', 'Squat deeper.']);
});

test('frames are streamed with strictly increasing numbers, and nothing is sent while nobody is visible', async () => {
  const { controller, socket } = await startController();
  for (let i = 0; i < 5; i++) controller.pushFrame(frame(170 - i));
  controller.pushFrame(null);
  const seqs = socket.poses.map((m) => m.sequence);
  assert.equal(seqs.length, 5);
  assert.deepEqual(seqs, [...seqs].sort((a, b) => a - b));
  assert.equal(new Set(seqs).size, 5);
  const ts = socket.poses.map((m) => m.timestamp_ms);
  assert.ok(ts.every((t, i) => i === 0 || t > ts[i - 1]));
  assert.equal(socket.poses[0].session_id, 'sess-1');
});

test('pushFrame returns the body position for the game, standing at the top and a deep squat at the bottom', async () => {
  const { controller } = await startController();
  let out = controller.pushFrame(frame(172));
  near(out.position, 0, 0.02);
  out = controller.pushFrame(frame(98));
  assert.equal(out.position, 1);
  out = controller.pushFrame(frame(132));
  assert.ok(out.position > 0.3 && out.position < 0.7);
});

test('losing the person is reported once, with guidance, and recovering is reported too', async () => {
  const { controller, log } = await startController({ options: { lostAfterFrames: 3 } });
  controller.pushFrame(frame(170));
  assert.deepEqual(log.tracking, ['ok']);
  for (let i = 0; i < 5; i++) assert.equal(controller.pushFrame(null).position, null);
  assert.deepEqual(log.tracking, ['ok', 'lost']);
  assert.deepEqual(log.feedback.filter(([, s]) => s === 'tracking'), [['Move into camera view.', 'tracking']]);
  assert.equal(controller.pushFrame(frame(170)).tracking, 'ok');
  assert.deepEqual(log.tracking, ['ok', 'lost', 'ok']);
});

test('a flicker of missing frames is not treated as lost tracking', async () => {
  const { controller, log } = await startController();
  controller.pushFrame(frame(170));
  controller.pushFrame(null);
  controller.pushFrame(null);
  controller.pushFrame(frame(168));
  assert.deepEqual(log.tracking, ['ok']);
});

test('the wrong joint in view counts as "not visible" for the game', async () => {
  const { controller } = await startController({ options: { lostAfterFrames: 2 } });
  for (let i = 0; i < 4; i++) assert.equal(controller.pushFrame(elbow(90)).position, null); // elbows only, but this is a squat game
  assert.equal(controller.tracking, 'lost');
});

test('server coaching messages and form hints reach the game', async () => {
  const { log, socket } = await startController();
  socket.serverSends({ type: 'coaching_feedback', session_id: 'sess-1', rep_count: 4, issue_code: 'reduced_depth', message: 'Try your previous range.', source: 'rules' });
  assert.deepEqual(log.feedback.at(-1), ['Try your previous range.', 'coach']);
  socket.serverSends({ type: 'tracking_status', session_id: 'sess-1', tracking: 'lost' });
  assert.deepEqual(log.feedback.at(-1), ['Move into camera view.', 'tracking']);
});

test('pause stops streaming and tells the server; resume continues', async () => {
  const { controller, socket } = await startController();
  controller.setPaused(true);
  controller.pushFrame(frame(160));
  assert.equal(socket.poses.length, 0);
  assert.deepEqual(socket.sent.at(-1), { schema_version: 1, type: 'user_signal', session_id: 'sess-1', signal: 'pause' });
  controller.setPaused(false);
  controller.pushFrame(frame(160));
  assert.equal(socket.poses.length, 1);
  assert.equal(socket.sent.filter((m) => m.type === 'user_signal').at(-1).signal, 'resume');
});

test('finishing saves the workout and returns what the server counted', async () => {
  const { controller, fetchImpl, log } = await startController();
  const done = await controller.finish();
  assert.deepEqual(done, { sessionId: 'sess-1', totalReps: 3 });
  assert.ok(fetchImpl.calls.some((c) => c.url.endsWith('/api/workouts/sess-1/finish') && c.method === 'POST'));
  assert.equal(log.status.at(-1), 'closed');
});

test('a dropped connection reconnects to the same workout and the count carries on without double counting', async () => {
  const { controller, log, socket } = await startController();
  socket.serverSends(repEvent(1));
  socket.serverSends(repEvent(2));
  socket.serverCloses(1006);
  await wait(10);
  const again = lastSocket();
  assert.notEqual(again, socket);
  assert.ok(again.url.endsWith('/ws/workouts/sess-1'));
  assert.equal(controller.status, 'reconnecting');
  again.serverSends({ type: 'session_ready', session_id: 'sess-1', rep_count: 2, exercise: 'squat', analysis_supported: true, paused: false, coaching_enabled: true });
  await wait(5);
  assert.equal(controller.status, 'ready');
  again.serverSends(repEvent(2)); // replay after reconnect
  again.serverSends(repEvent(3));
  assert.deepEqual(log.reps.map((r) => r.repNumber), [1, 2, 3]);
});

test('closing on purpose does not try to reconnect', async () => {
  const { controller } = await startController();
  const count = FakeWebSocket.instances.length;
  controller.close();
  await wait(10);
  assert.equal(FakeWebSocket.instances.length, count);
  assert.equal(controller.status, 'closed');
});

test('a push-up round talks about push-ups and uses the elbow', async () => {
  const { controller, fetchImpl, socket } = await startController({
    exercise: 'push-up',
    routes: { 'POST /api/workouts': { status: 201, body: { session_id: 'sess-1', exercise: 'push-up', analysis_supported: true, status: 'active' } } },
  });
  assert.deepEqual(fetchImpl.calls[0].body, { exercise: 'push-up' });
  const out = controller.pushFrame(elbow(165));
  near(out.position, 0, 0.05);
  assert.equal(controller.pushFrame(elbow(90)).position, 1);
  assert.equal(socket.poses.length, 2);
});

test('an exercise the server cannot count is refused with a clear message', async () => {
  FakeWebSocket.instances.length = 0;
  const controller = new ExerciseController({
    exercise: 'squat',
    apiUrl: 'http://api.test',
    getToken: async () => 'tok',
    fetchImpl: fakeFetch({ 'POST /api/workouts': { status: 201, body: { session_id: 's', exercise: 'squat', analysis_supported: false, status: 'active' } } }),
    WebSocketImpl: FakeWebSocket,
  });
  await assert.rejects(controller.start(), /cannot count/);
  assert.equal(controller.status, 'error');
});

test('camera / network failures surface as errors the screen can show', async () => {
  const controller = new ExerciseController({
    exercise: 'squat',
    apiUrl: 'http://api.test',
    getToken: async () => 'tok',
    fetchImpl: fakeFetch({ 'POST /api/workouts': { status: 401, body: { detail: 'invalid or expired token' } } }),
    WebSocketImpl: FakeWebSocket,
  });
  await assert.rejects(controller.start(), /401/);
  assert.equal(controller.status, 'error');
});

// ------------------------------------------------------------------ results client and rooms client

test('saving a result posts the body with the token and surfaces a missing migration clearly', async () => {
  const body = { game: 'rep_racer', mode: 'solo', exercise: 'squat', workout_session_id: 'w', duration_ms: 1, target_reps: 5, time_limit_s: 60 };
  const ok = fakeFetch({ 'POST /api/games/results': { status: 201, body: { id: 'r1', valid_reps: 5 } } });
  assert.equal((await saveGameResult('http://api.test/', 'tok', body, ok)).id, 'r1');
  assert.equal(ok.calls[0].url, 'http://api.test/api/games/results');
  assert.deepEqual(ok.calls[0].body, body);
  const missing = fakeFetch({ 'POST /api/games/results': { status: 503, body: { detail: 'GRIT Games tables are missing: apply supabase/migrations/x.sql' } } });
  await assert.rejects(saveGameResult('http://api.test', 'tok', body, missing), (e) => e instanceof GamesApiError && e.tablesMissing === true);
  const conflict = fakeFetch({ 'POST /api/games/results': { status: 409, body: { detail: 'finish the workout first' } } });
  await assert.rejects(saveGameResult('http://api.test', 'tok', body, conflict), (e) => e.status === 409 && !e.tablesMissing);
});

test('the personal best request carries the game, exercise and target', async () => {
  const f = fakeFetch({ 'GET /api/games/best': { body: { best: null, attempts: 0 } } });
  assert.deepEqual(await getBest('http://api.test', 'tok', 'rep_racer', 'push-up', 10, f), { best: null, attempts: 0 });
  assert.equal(f.calls[0].url, 'http://api.test/api/games/best?game=rep_racer&exercise=push-up&target_reps=10');
});

test('room events are parsed and anything else is ignored', () => {
  assert.equal(parseRoomEvent('{"type":"room_state","room":"ABC123"}').type, 'room_state');
  assert.equal(parseRoomEvent('{"type":"mystery"}'), null);
  assert.equal(parseRoomEvent('nope'), null);
});

test('creating and joining a room use the right endpoints; errors carry the server code', async () => {
  const f = fakeFetch({
    'POST /api/games/rooms': { status: 201, body: { code: 'ABC234', exercise: 'squat', target_reps: 5, time_limit_s: 60, status: 'lobby', host_id: 'u', players: 1 } },
    'POST /api/games/rooms/ABC234/join': { body: { code: 'ABC234', players: 2 } },
    'POST /api/games/rooms/FULL99/join': { status: 409, body: { detail: { code: 'room_full', detail: 'this room already has two players' } } },
  });
  const room = await createRoom('http://api.test', 'tok', { exercise: 'squat', targetReps: 5, timeLimitS: 60, displayName: 'Ann' }, f);
  assert.equal(room.code, 'ABC234');
  assert.deepEqual(f.calls[0].body, { exercise: 'squat', target_reps: 5, time_limit_s: 60, display_name: 'Ann' });
  assert.equal((await joinRoom('http://api.test', 'tok', ' abc234 ', 'Bo', f)).players, 2);
  await assert.rejects(joinRoom('http://api.test', 'tok', 'FULL99', 'Bo', f), /already has two players/);
});

const roomState = (over = {}) => ({
  type: 'room_state', room: 'ABC234', you: 'u1', race_id: 0, exercise: 'squat', target_reps: 5, time_limit_s: 60, status: 'lobby', host_id: 'u1',
  starts_in_ms: null, elapsed_ms: 0, remaining_ms: null, players: [], winner_id: null, end_reason: null, results: [], ...over,
});

async function joinSession(extra = {}) {
  FakeWebSocket.instances.length = 0;
  const seen = { states: [], events: [], feedback: [], tracking: [], conn: [] };
  const session = new RoomSession({
    apiUrl: 'http://api.test',
    code: 'ABC234',
    exercise: 'squat',
    getToken: async () => 'tok',
    WebSocketImpl: FakeWebSocket,
    reconnectDelayMs: 0,
    lostAfterFrames: 3,
    handlers: {
      onState: (s) => seen.states.push(s),
      onEvent: (e) => seen.events.push(e),
      onFeedback: (m, s) => seen.feedback.push([m, s]),
      onTracking: (t) => seen.tracking.push(t),
      onConnection: (c, d) => seen.conn.push([c, d]),
    },
    ...extra,
  });
  await session.connect();
  await wait();
  lastSocket().serverSends(roomState());
  return { session, seen, socket: lastSocket() };
}

test('a room session authenticates, shows the first state, and sends ready', async () => {
  const { session, seen, socket } = await joinSession();
  assert.ok(socket.url.endsWith('/ws/games/rooms/ABC234'));
  assert.deepEqual(socket.sent[0], { schema_version: 1, type: 'auth', access_token: 'tok' });
  assert.equal(seen.states.length, 1);
  session.setReady(true);
  assert.deepEqual(socket.sent.at(-1), { schema_version: 1, type: 'ready', ready: true });
  session.rematch();
  assert.equal(socket.sent.at(-1).type, 'rematch');
});

test('room frames carry only angles and keep strictly increasing numbers across a rematch', async () => {
  const { session, socket } = await joinSession();
  for (let i = 0; i < 4; i++) {
    session.pushFrame(frame(170 - i), 1000 + i * 40);
    await wait(40); // the throttle is ~30 per second
  }
  socket.serverSends({ type: 'race_started', room: 'ABC234', race_id: 1, time_limit_s: 60, target_reps: 5 });
  await wait(40);
  session.pushFrame(frame(150), 5000);
  const poses = socket.sent.filter((m) => m.type === 'pose_update');
  assert.ok(poses.length >= 4);
  const seqs = poses.map((m) => m.sequence);
  assert.ok(seqs.every((s, i) => i === 0 || s > seqs[i - 1]), `sequence numbers: ${seqs}`);
  assert.ok(poses.every((m) => m.session_id === undefined && m.data && m.schema_version === 1), 'no session id, no video: just angles');
});

test('room tracking loss is reported with guidance, and recovery too', async () => {
  const { session, seen } = await joinSession();
  session.pushFrame(frame(170));
  for (let i = 0; i < 4; i++) session.pushFrame(null);
  assert.deepEqual(seen.tracking, ['ok', 'lost']);
  assert.deepEqual(seen.feedback.at(-1), ['Move into camera view.', 'tracking']);
  session.pushFrame(frame(170));
  assert.deepEqual(seen.tracking, ['ok', 'lost', 'ok']);
});

test('a dropped room connection reconnects to the same seat with a fresh token', async () => {
  let n = 0;
  const { seen, socket } = await joinSession({ getToken: async () => `tok-${++n}` });
  socket.serverCloses(1006);
  await wait(15);
  const again = lastSocket();
  assert.notEqual(again, socket);
  await wait();
  assert.equal(again.sent[0].access_token, 'tok-2');
  again.serverSends(roomState({ status: 'racing' }));
  assert.deepEqual(seen.conn.map(([c]) => c), ['connecting', 'open', 'reconnecting', 'open']);
});

test('losing the room (404) or the seat (taken over) does not loop reconnecting', async () => {
  for (const code of [4404, 4410]) {
    const { seen, socket } = await joinSession();
    const count = FakeWebSocket.instances.length;
    socket.serverCloses(code);
    await wait(15);
    assert.equal(FakeWebSocket.instances.length, count, `no reconnect after ${code}`);
    assert.equal(seen.conn.at(-1)[0], 'error');
  }
});

test('leaving tells the server and closes quietly', async () => {
  const { session, seen, socket } = await joinSession();
  session.leave();
  assert.deepEqual(socket.sent.at(-1), { schema_version: 1, type: 'leave' });
  await wait(15);
  assert.equal(seen.conn.at(-1)[0], 'closed');
});

// ------------------------------------------------------------------ wiring that unit tests can still check

test('GRIT Games is reachable from the app: routes exist, the root layout declares them, Home links to them', () => {
  const read = (p) => readFileSync(new URL(p, import.meta.url), 'utf8');
  for (const route of ['../app/games.tsx', '../app/games/racer.tsx']) {
    assert.ok(existsSync(new URL(route, import.meta.url)), `${route} is missing`);
  }
  for (const name of ['games', 'games/racer']) assert.match(read('../app/_layout.tsx'), new RegExp(`name="${name}"`));
  assert.match(read('../app/(tabs)/index.tsx'), /\/games/);
  assert.match(read('../components/app-tabs.web.tsx'), /\/games/);
  assert.match(read('../app/(tabs)/index.tsx'), /\/pose/, 'the existing Exercises (/pose) button is still there');
});

test('no API key or secret is referenced from the games code', () => {
  for (const f of ['exercise.ts', 'exercise-controller.ts', 'games-api.ts', 'room-client.ts', 'racer-engine.ts']) {
    const text = readFileSync(new URL(f, import.meta.url), 'utf8');
    assert.doesNotMatch(text, /GEMINI_API_KEY|ANTHROPIC_API_KEY|SUPABASE_SECRET|sb_secret_/i, f);
  }
});

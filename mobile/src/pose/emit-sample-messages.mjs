// Prints, as JSON, the exact messages this TypeScript code sends to the backend, so the BACKEND's test-suite
// (server/tests/test_frontend_contract.py) can check them against the real server models and feed them to the real
// server. Usage (from mobile/):  node --experimental-strip-types --no-warnings --import ./src/pose/test-register.mjs \
//                                  src/pose/emit-sample-messages.mjs <session-uuid>
import { computePoseAngles, LM } from './angles.ts';
import { PoseStreamer } from './protocol.ts';
import { simulatedPushUpFrames, simulatedSquatFrames } from './simulate.ts';
import { WorkoutSocket } from './workout-client.ts';

const sessionId = process.argv[2] ?? '00000000-0000-0000-0000-000000000000';

// 1) A simulated 5-squat stream (depth drops on the last two reps), numbered and stamped by the real streamer.
let clock = 1_800_000_000_000;
const streamer = new PoseStreamer(sessionId, 30, () => clock);
const stream = [];
for (const frame of simulatedSquatFrames({ depths: [85, 85, 85, 98, 98] })) {
  const update = streamer.next(frame);
  if (update) stream.push(update);
  clock += 34; // ~29 fps, just inside the 30 fps cap
}

// 1b) The same for 5 push-ups (elbow angles; depth drops on the last two).
clock = 1_800_000_000_000;
const pushStreamer = new PoseStreamer(sessionId, 30, () => clock);
const pushups = [];
for (const frame of simulatedPushUpFrames({ depths: [80, 80, 80, 95, 95] })) {
  const update = pushStreamer.next(frame);
  if (update) pushups.push(update);
  clock += 34;
}

// 2) What the socket sends: the auth handshake and the three user signals.
class RecordingSocket {
  constructor() { this.sent = []; this.readyState = 0; }
  send(data) { this.sent.push(JSON.parse(data)); }
  close() {}
}
let recorder;
const sock = new WorkoutSocket('http://localhost:8000', sessionId, 'a-token', { onEvent() {} }, class extends RecordingSocket {
  constructor() { super(); recorder = this; }
});
sock.connect();
recorder.readyState = 1;
recorder.onopen();
recorder.onmessage({ data: JSON.stringify({ type: 'session_ready', session_id: sessionId, rep_count: 0 }) });
for (const s of ['pause', 'resume', 'pain']) sock.sendSignal(sessionId, s);
const [auth, ...signals] = recorder.sent;

// 3) A frame with elbow and hip angles measured from landmarks (the non-squat exercises' data).
const lm = Array.from({ length: 33 }, () => ({ x: 0, y: 0, z: 0, visibility: 0.9 }));
lm[LM.LEFT_SHOULDER] = { x: 0, y: 1, z: 0, visibility: 0.9 };
lm[LM.LEFT_ELBOW] = { x: 0, y: 0, z: 0, visibility: 0.9 };
lm[LM.LEFT_WRIST] = { x: 1, y: 0, z: 0, visibility: 0.9 };
lm[LM.LEFT_HIP] = { x: 0, y: 2, z: 0, visibility: 0.9 };
lm[LM.LEFT_KNEE] = { x: 0, y: 3, z: 0, visibility: 0.9 };
const upper = new PoseStreamer(sessionId, 30, () => clock).next(computePoseAngles(lm));

console.log(JSON.stringify({ stream, pushups, auth, signals, upper }));

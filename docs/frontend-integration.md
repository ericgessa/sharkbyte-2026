# Frontend integration guide (Expo / React Native)

How the mobile app talks to the backend. Message shapes are in [event-contracts.md](event-contracts.md);
this page is the practical "how do I connect" part.

## 1. Configure (3 values)

Copy `mobile/.env.example` to `mobile/.env` (git-ignored) and fill it in. Expo only exposes variables that
start with `EXPO_PUBLIC_`, and **inlines them into the app bundle**, so never put the Supabase *secret* key
there. After editing `.env`, fully reload the app (Expo Go: shake > Reload).

| Variable | Value |
| --- | --- |
| `EXPO_PUBLIC_API_URL` | backend base URL, see below |
| `EXPO_PUBLIC_SUPABASE_URL` | `https://djphcmzoldlmzoosgcej.supabase.co` (public) |
| `EXPO_PUBLIC_SUPABASE_PUBLISHABLE_KEY` | `sb_publishable_...`, ask the backend dev or copy it from Supabase Dashboard > Project Settings > API Keys |

### Which `EXPO_PUBLIC_API_URL`?

| Where the app runs | URL |
| --- | --- |
| Physical phone, same Wi-Fi as the backend laptop | `http://<backend laptop LAN IP>:8000` (the backend dev's `scripts.dev_server` prints it) |
| Android emulator | `http://10.0.2.2:8000` (the emulator's alias for the host machine) |
| iOS simulator, or Expo web on the backend laptop | `http://localhost:8000` |
| Different network / remote | the `https://...` tunnel URL the backend dev shares |

WebSocket URL = the same base with `http` -> `ws` and `https` -> `wss`.

If a phone on the same Wi-Fi can't connect, see Troubleshooting. Event Wi-Fi often blocks device-to-device
traffic ("client isolation"), and then only a tunnel will work.

## 2. Sign in with Supabase

Install (from `mobile/`, per Supabase's Expo guide and this repo's AGENTS.md rule to use `expo install`):

```bash
npx expo install @supabase/supabase-js react-native-url-polyfill expo-sqlite
```

`lib/supabase.ts`:

```ts
import 'react-native-url-polyfill/auto'
import { createClient } from '@supabase/supabase-js'
import 'expo-sqlite/localStorage/install'

export const supabase = createClient(
  process.env.EXPO_PUBLIC_SUPABASE_URL!,
  process.env.EXPO_PUBLIC_SUPABASE_PUBLISHABLE_KEY!,
  { auth: { storage: localStorage, autoRefreshToken: true, persistSession: true, detectSessionInUrl: false } },
)
```

Sign up / in with the standard supabase-js calls:

```ts
await supabase.auth.signUp({ email, password })              // may require email confirmation (see below)
await supabase.auth.signInWithPassword({ email, password })

// Always read the token fresh: it expires (~1 h) and supabase-js refreshes it for you.
const { data } = await supabase.auth.getSession()
const accessToken = data.session?.access_token
```

A profile row is created automatically on sign-up (database trigger); the app doesn't create it.

> Email confirmation: if the Supabase project has "Confirm email" on, new users can't sign in until they click
> the emailed link. For demos the backend dev can turn it off in Dashboard > Authentication.

## Getting your first token (start here if you see 401 / "needs token")

Every `/api/*` call needs the **logged-in user's Supabase access token**. The publishable key is *not* a token
(sending it as one gives a 401). No account means no token, so:

1. **Get a user account.** Either sign up in the app with `supabase.auth.signUp(...)`, or ask the backend dev to
   create a test user in the Supabase dashboard (Authentication > Users > Add user, with auto-confirm ticked) and
   share the email/password privately. If the project has "Confirm email" turned on, a self-signed-up user must
   click the emailed link before `signInWithPassword` works, and Supabase's built-in email sending is
   rate-limited, so for hackathon testing a dashboard-created or auto-confirmed user is the reliable route.
2. **Sign in** (`supabase.auth.signInWithPassword`) and use `data.session.access_token` as the Bearer token.

**Test the API without any app code** (replace the placeholders; `<API>` is `EXPO_PUBLIC_API_URL`):

```bash
# 1) trade email + password for an access token (the apikey header is the PUBLISHABLE key)
curl -s -X POST "https://djphcmzoldlmzoosgcej.supabase.co/auth/v1/token?grant_type=password" \
  -H "apikey: <publishable key>" -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"your-password"}'
# copy the "access_token" value from the JSON it prints (it lasts about an hour)

# 2) call the backend with it
curl -s <API>/api/workouts -H "Authorization: Bearer <access_token>"
# -> [] for a new user. With no or a wrong token you get a 401 that says what is wrong.
```

Three different things get mixed up, so to be explicit: the **publishable key** identifies the *app* to Supabase
(public, goes in `.env`); the **access token** identifies the *user* (short-lived, comes from signing in, goes in
the `Authorization` header); the **secret key** must never leave the backend. If you call Supabase's own database
API directly instead of this backend, it needs both (`apikey` = publishable key, `Authorization: Bearer` = the
access token) and only returns the signed-in user's own rows; the backend is the intended route for workouts.

Seeing nothing at all (timeouts, "network request failed") is a different problem from a 401: a 401 means you
reached the backend. Open `<API>/health` in the phone's browser first.

## 3. REST calls

Every `/api/*` request needs `Authorization: Bearer <access token>`.

```ts
const API = process.env.EXPO_PUBLIC_API_URL!

async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const { data } = await supabase.auth.getSession()
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${data.session?.access_token}`, ...init.headers },
  })
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  if (res.status === 204) return undefined as T   // DELETE succeeds with no body
  return res.json() as Promise<T>
}

const workout = await api<{ session_id: string }>('/api/workouts', { method: 'POST', body: JSON.stringify({ exercise: 'squat' }) })
// ... stream reps over the WebSocket ...
const summary = await api(`/api/workouts/${workout.session_id}/finish`, { method: 'POST' })   // { status: 'completed', total_reps, ... }
```

**Exercise picker.** Don't hardcode exercise names: fetch them (no login needed), so new exercises appear without
an app release:

```ts
const exercises = await fetch(`${API}/api/exercises`).then(r => r.json())
// [{ slug: 'squat', display_name: 'Squat', analysis_supported: true, aliases: [...], primary_angles: [...] }, ...]
await api('/api/workouts', { method: 'POST', body: JSON.stringify({ exercise: picked.slug }) })
```

Check `analysis_supported` (also on every workout and in `session_ready`). Today only **squat** is `true`: the server
counts reps and coaches it. For the other exercises (push-up, pull-up, deadlift, bench press, lunge, plank) the
workout is logged and the camera/ESP32 data is recorded, but **the server does not count reps or give form feedback**,
so don't show a live rep counter or promise coaching for them; show something like "Tracking coming soon". Pain/stop
and pause work for every exercise. Don't try to count reps client-side and send them: the API has no way to accept
them. A wrong exercise name is a 422 whose message lists the valid ones.

**End-of-workout recap.** `GET /api/workouts/{id}/coach-summary` returns `{ summary, source }`: a few plain sentences
about the workout, ready to show under the stats. `source` is `"agent"` when an LLM wrote it and `"rules"` for the
deterministic version (either is fine to show as-is). Call it once when the workout ends and cache it client-side; it is
limited to about 6 requests per minute.

Screens you can build from the other read endpoints (response shapes in [event-contracts.md](event-contracts.md)):

```ts
const history = await api<any[]>('/api/workouts?limit=20')                     // history list, newest first
const summary = await api<any>(`/api/workouts/${id}/summary`)                  // reps + coaching messages + stats
```

Deleting a workout (history screen, "delete" action): `await api(`/api/workouts/${id}`, { method: 'DELETE' })`. It is
permanent and removes the reps, coaching messages **and** the stored sensor data, so ask the user to confirm. The
`api` helper above throws on non-2xx and returns `undefined` for the body-less 204. A `503` means the sensor data couldn't be deleted right now and nothing was removed: tell the
user to try again. A `429` means too many requests: wait the number of seconds in the `Retry-After` header.

Status codes: `401` token missing/expired, `404` session not found (or not yours), `503` on finish means
some data didn't save yet: wait a second and call finish again (safe to repeat).

Interactive REST docs while the backend runs: `<API>/docs`. WebSocket messages aren't listed there.

## 4. Live workout over WebSocket

```ts
function openWorkoutSocket(sessionId: string, accessToken: string, onEvent: (e: any) => void) {
  const ws = new WebSocket(`${API.replace(/^http/, 'ws')}/ws/workouts/${sessionId}`)
  let ready = false
  let sequence = 0

  // 1) The FIRST message must be auth (token is sent here, not in the URL).
  ws.onopen = () => ws.send(JSON.stringify({ schema_version: 1, type: 'auth', access_token: accessToken }))

  ws.onmessage = (m) => {
    const e = JSON.parse(m.data as string)
    if (e.type === 'session_ready') ready = true          // 2) only now start sending poses
    onEvent(e)  // rep_completed | coaching_feedback | tracking_status | error
  }

  // 3) Send one per processed camera frame (<= ~30/s is plenty).
  const sendPose = (leftKneeDeg: number | null, rightKneeDeg: number | null, confidence: number) => {
    if (!ready || ws.readyState !== WebSocket.OPEN) return
    ws.send(JSON.stringify({
      schema_version: 1, type: 'pose_update', session_id: sessionId,
      sequence: sequence++, timestamp_ms: Date.now(),
      data: { left_knee_angle_deg: leftKneeDeg, right_knee_angle_deg: rightKneeDeg, landmark_confidence: confidence },
    }))
  }
  return { ws, sendPose }
}
```

Rules that matter:
- `sequence` must strictly increase within a session; repeats/lower values are dropped.
- Knee angle in degrees, 0-180 (180 = straight leg). Send `null` for a knee that isn't visible; at least one
  of the two must be present. Don't guess angles for hidden joints.
- `landmark_confidence` 0-1; below 0.6 the frame is ignored and you get one `tracking_status: lost`
  (then `recovered`). Use that to show "step back so we can see your legs".
- Show `coaching_feedback.message` as-is (text and/or speech). It is rate-limited server-side. When the server's AI
  coach is on, a cue can arrive a few seconds after its `rep_completed` (or not at all); don't wait for it before
  updating the rep count.
- **ESP32 data:** if the phone talks to the wearable over Bluetooth, forward each sample over this same socket
  (no separate endpoint or credentials): `ws.send(JSON.stringify({ schema_version: 1, type: 'sensor_update',
  session_id: sessionId, device_id: 'esp32-01', sequence: n++, timestamp_ms: deviceMillis, data: { flex_raw, imu_pitch_deg } }))`.
  Use the device's own clock for `timestamp_ms`; the server aligns it. Nothing comes back for these messages.
- Add **Pause/Resume** and a clearly visible **"I'm in pain / stop"** button. They send a `user_signal`
  (`'pause' | 'resume' | 'pain'`), and the server answers with `session_state`:
  ```ts
  const sendSignal = (signal: 'pause' | 'resume' | 'pain') =>
    ws.send(JSON.stringify({ schema_version: 1, type: 'user_signal', session_id: sessionId, signal }))
  ```
  After `pain` the server stops technique coaching for the whole session (`coaching_enabled: false`). Show
  the returned message and don't encourage the user to continue.
- Reconnect is safe: call `openWorkoutSocket` again with the same session id; the rep count continues.
  The server closes a connection after 3 hours with `4401` so the login is re-checked: on `4401`, refresh the token
  and reconnect (long workouts will hit this once).
- Close codes: `4401` bad/expired token (refresh and retry), `4404` unknown/not yours, `4409` session already finished,
  `4410` replaced by a newer connection (do NOT auto-reconnect on this one, or two copies of the app will fight),
  `1009` message over 16 KB. Reconnecting after a dropped connection is safe and takes over immediately.
- Keep camera messages to ~30/s. Over ~300 messages/s the server drops the extras and sends one `rate_limited` error.

## 5. Testing without the camera

**Quick reachability test from your phone (30 seconds):** open `<EXPO_PUBLIC_API_URL>/health` in the phone's
browser. `{"status":"ok"}` means the phone can reach the backend; anything else is a network problem (wrong IP,
different Wi-Fi, firewall, or Wi-Fi client isolation), not an app bug.

The backend dev can also check the server end to end the way the app uses it (real sign-in, REST, WebSocket,
simulated squats, clearly synthetic): `python -m scripts.check_remote --base-url <API URL>` from `server/`.
`python -m scripts.e2e_live` does the same in-process, without a running server.

## 6. MediaPipe pose tracking in the browser (Pose lab)

`mobile/src/pose/` turns camera frames into the `pose_update` messages of section 4, and `/pose` is a developer
page that wires it all together (camera, skeleton overlay, backend, coaching cues). **Web only for now**; on a
phone the screen says so (native camera + MediaPipe is not built).

```bash
cd mobile
npm ci
npx expo start --web        # then open http://localhost:8081/pose
```

What it does: loads MediaPipe's Pose Landmarker **in the browser** (nothing leaves the device), computes joint
angles from the 3D world landmarks (`angles.ts`: knees, elbows, hips, `null` when a joint is not visible, plus a
`landmark_confidence`), numbers and rate-limits the frames to 30/s (`protocol.ts`), and streams them over the
authenticated WebSocket (`workout-client.ts`). Paste a Supabase **access token** (section 2) and the API URL into
the lab, then *Start workout*. The same code is reusable in the real screens; the lab is just the harness.

Things to know:

- **Needs internet.** The library (`cdn.jsdelivr.net`), the WebAssembly runtime and the ~6 MB model
  (`storage.googleapis.com`) are fetched by the browser at runtime. They are not bundled because Metro cannot
  bundle `@mediapipe/tasks-vision` (it contains `import(<expression>)`); the npm package is only used for types.
  The version is pinned in `package.json` and in `mediapipe.web.ts` (a test checks they agree).
- **Camera needs a secure context:** `http://localhost` or `https://`. Opening the dev server by LAN IP over plain
  `http://` will not get camera access; use localhost or an HTTPS tunnel.
- **No camera?** *Simulate 5 squats* streams synthetic angles through the same path, and *Detect pose in image*
  runs the real model on a still image (the host must allow CORS, e.g. `upload.wikimedia.org`).
- **Route registration:** with `expo-router/ui` Tabs, a route only works if it has a `TabTrigger`. `/pose` has a
  hidden one in `src/components/app-tabs.web.tsx` (no visible tab). Without it the router bounces `/pose` to `/`.
  Any new non-tab page on web needs the same.
- Don't start the dev server with `CI=1`: that disables file watching, so edits are silently not served.
- Tests: `npm run test:pose` (27 pure-logic tests, no browser). The backend suite also runs the TypeScript
  (`server/tests/test_frontend_contract.py`) and checks that what it produces is accepted by the real server.

Verified so far: model loads in a browser (GPU delegate), real inference on a still image returns angles, and the
simulated stream through the lab reaches the live backend (5 reps counted, the depth cue comes back). **Not
verified:** a live camera with a person moving, accuracy of the angles against real squats, mobile devices,
frame-rate on slower machines.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `Network request failed` on a phone | Wrong `EXPO_PUBLIC_API_URL` (`localhost` on a phone means the phone itself), phone not on the same Wi-Fi, Windows Firewall blocking port 8000 on the backend laptop, or Wi-Fi client isolation. Try opening `<API>/health` in the phone's browser first |
| Works on web, not on phone (or vice versa) | Web is subject to CORS (backend allows it); native isn't. The usual native culprit is the URL or firewall |
| `401` on every call | Token missing/expired: re-read it with `supabase.auth.getSession()` right before the call |
| Android blocks plain `http://` | Use the `https://` tunnel URL instead |
| Env change not picked up | Fully reload the app after editing `mobile/.env` |
| WebSocket closes immediately with `4401` | First message wasn't `auth`, or the token is bad |

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
  return res.json() as Promise<T>
}

const workout = await api<{ session_id: string }>('/api/workouts', { method: 'POST', body: JSON.stringify({ exercise: 'squat' }) })
// ... stream reps over the WebSocket ...
const summary = await api(`/api/workouts/${workout.session_id}/finish`, { method: 'POST' })   // { status: 'completed', total_reps, ... }
```

Screens you can build from the other read endpoints (response shapes in [event-contracts.md](event-contracts.md)):

```ts
const history = await api<any[]>('/api/workouts?limit=20')                     // history list, newest first
const summary = await api<any>(`/api/workouts/${id}/summary`)                  // reps + coaching messages + stats
```

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
- Show `coaching_feedback.message` as-is (text and/or speech). It is rate-limited server-side.
- Reconnect is safe: call `openWorkoutSocket` again with the same session id; the rep count continues.
- Close codes: `4401` bad/expired token (refresh and retry), `4404` unknown/not yours, `4409` session already finished.

## 5. Testing without the camera

The backend dev can stream simulated squats (clearly synthetic) to check your UI:
`python -m scripts.e2e_live` (from `server/`) runs the whole flow against Supabase with a throwaway user.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `Network request failed` on a phone | Wrong `EXPO_PUBLIC_API_URL` (`localhost` on a phone means the phone itself), phone not on the same Wi-Fi, Windows Firewall blocking port 8000 on the backend laptop, or Wi-Fi client isolation. Try opening `<API>/health` in the phone's browser first |
| Works on web, not on phone (or vice versa) | Web is subject to CORS (backend allows it); native isn't. The usual native culprit is the URL or firewall |
| `401` on every call | Token missing/expired: re-read it with `supabase.auth.getSession()` right before the call |
| Android blocks plain `http://` | Use the `https://` tunnel URL instead |
| Env change not picked up | Fully reload the app after editing `mobile/.env` |
| WebSocket closes immediately with `4401` | First message wasn't `auth`, or the token is bad |

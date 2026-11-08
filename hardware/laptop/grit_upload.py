"""Send the spine wearable's readings to the GRIT server, which stores them in Tiger Data.

Used by spine_receiver.py (``--upload``). The server's contract is docs/event-contracts.md (`sensor_update`): the
receiver signs in, starts a workout, opens that workout's WebSocket and forwards each live reading.

    base station line:  {"type":"data","t":4567,"phase":"live","torso":12.3,"upper":1.2,"mid":-0.4,"lumbar":5.6,...}
    sensor_update data: {"imu_pitch_deg":12.3,"spine_upper_deg":1.2,"spine_mid_deg":-0.4,"spine_lumbar_deg":5.6}

Sensor-only: the server stores every reading (Tiger Data) and answers with the sensor-based coaching. Back-shape cues
for squats and push-ups need the CAMERA to count the reps, which happens in the browser; for that, connect the
wearable on the /pose page instead (it forwards both over one workout socket, the only connection the server allows).

Only the standard library plus `websockets` (pip install websockets). Your password is read with getpass (or
GRIT_EMAIL / GRIT_PASSWORD, or a ready token in GRIT_TOKEN); it is never printed, stored or put on a command line.
"""

from __future__ import annotations

import getpass
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable

# base station key -> sensor_update field, and the server's limits for each (server/app/schemas/sensors.py)
FIELDS = {
    "torso": ("imu_pitch_deg", 360.0),
    "upper": ("spine_upper_deg", 180.0),
    "mid": ("spine_mid_deg", 180.0),
    "lumbar": ("spine_lumbar_deg", 180.0),
}
MIN_INTERVAL_MS = 40  # at most 25 samples a second, by the wearable's own clock


def line_to_sample(msg: dict[str, Any]) -> tuple[int, dict[str, float]] | None:
    """A base-station message -> (wearable_ms, sensor_update data), or None if it carries nothing usable.

    Only ``phase == "live"`` lines count: before that the wearable is still calibrating and its numbers are not relative
    to a neutral posture. A value that is null, not a number, or beyond the server's limit is left out (the server would
    reject the whole message for it).
    """
    if msg.get("type") != "data" or msg.get("phase") != "live":
        return None
    t = msg.get("t")
    if not isinstance(t, int) or isinstance(t, bool) or t < 0:
        return None
    data: dict[str, float] = {}
    for key, (field, limit) in FIELDS.items():
        v = msg.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) <= limit:
            data[field] = float(v)
    return (t, data) if data else None


class ServerRejected(RuntimeError):
    """The server answers every reading with an error: reconnecting will not help."""


class Uploader:
    """One workout's worth of readings, sent to the server. ``connect`` is injectable for tests."""

    def __init__(self, api: str, exercise: str = "squat", device_id: str = "esp32-spine", connect: Callable[[str], Any] | None = None) -> None:
        self.api = api.rstrip("/")
        self.exercise = exercise
        self.device_id = device_id
        self._connect = connect or _ws_connect
        self.token = ""
        self.refresh_token = ""
        self.token_at = 0.0
        self.session_id = ""
        self.sent = 0
        self._seq = 0
        self._last_ms: int | None = None
        self._ws: Any = None
        self._cfg: dict[str, str] = {}
        self.rejected = 0
        self.last_error = ""

    # ---- http helpers (standard library only)
    def _http(self, method: str, url: str, body: Any = None, headers: dict[str, str] | None = None) -> Any:
        req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = str(json.loads(e.read()).get("detail", ""))[:200]
            except Exception:
                pass
            raise RuntimeError(f"{method} {url} -> {e.code} {detail}".strip()) from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"cannot reach {self.api}: {e.reason}. Is the server running?") from None

    def sign_in(self) -> None:
        """Get an access token from a ready one (GRIT_TOKEN) or by signing in with the project's public key."""
        ready = os.environ.get("GRIT_TOKEN", "").strip()
        if ready:
            self.token, self.token_at = ready, time.monotonic()
            return
        cfg = self._http("GET", f"{self.api}/api/config")  # public Supabase URL + publishable key; never a secret
        email = os.environ.get("GRIT_EMAIL") or input("GRIT account email: ").strip()
        password = os.environ.get("GRIT_PASSWORD") or getpass.getpass("Password (not shown): ")
        res = self._http("POST", f"{cfg['supabase_url']}/auth/v1/token?grant_type=password", {"email": email, "password": password}, {"apikey": cfg["supabase_publishable_key"]})
        self.token, self.refresh_token, self.token_at = res["access_token"], res.get("refresh_token", ""), time.monotonic()
        self._cfg = cfg

    def _refresh(self) -> None:
        if not self.refresh_token:
            return
        cfg = self._cfg
        res = self._http("POST", f"{cfg['supabase_url']}/auth/v1/token?grant_type=refresh_token", {"refresh_token": self.refresh_token}, {"apikey": cfg["supabase_publishable_key"]})
        self.token, self.refresh_token, self.token_at = res["access_token"], res.get("refresh_token", self.refresh_token), time.monotonic()

    def start(self, session_id: str | None = None) -> str:
        """Sign in, start (or reuse) a workout, and open its WebSocket. Returns the workout id."""
        self.sign_in()
        if session_id:
            self.session_id = session_id
        else:
            created = self._http("POST", f"{self.api}/api/workouts", {"exercise": self.exercise}, {"Authorization": f"Bearer {self.token}"})
            self.session_id = created["session_id"]
        self._open()
        return self.session_id

    def _open(self) -> None:
        self._ws = self._connect(f"{self.api.replace('http', 'ws', 1)}/ws/workouts/{self.session_id}")
        self._ws.send(json.dumps({"schema_version": 1, "type": "auth", "access_token": self.token}))
        first = json.loads(self._ws.recv())
        if first.get("type") != "session_ready":
            raise RuntimeError(f"the server did not accept the workout socket: {first}")

    # ---- streaming
    def add(self, msg: dict[str, Any]) -> bool:
        """Forward one base-station message if it is a usable live sample. Returns True if it was sent."""
        sample = line_to_sample(msg)
        if sample is None:
            return False
        t, data = sample
        if self._last_ms is not None and 0 <= t - self._last_ms < MIN_INTERVAL_MS:
            return False  # faster than 25 Hz; the next sample will do
        self._last_ms = t
        payload = json.dumps({"schema_version": 1, "type": "sensor_update", "session_id": self.session_id, "device_id": self.device_id, "sequence": self._seq, "timestamp_ms": t, "data": data})
        self._seq += 1
        try:
            self._send(payload)
        except ServerRejected:
            raise
        except Exception:
            # the socket dropped or the token expired (about an hour): refresh and reopen once, then carry on
            try:
                self._refresh()
                self._open()
                self._send(payload)
            except Exception as e:
                raise RuntimeError(f"lost the connection to the server: {e}") from None
        self.sent += 1
        return True

    def _send(self, payload: str) -> None:
        self._ws.send(payload)
        self._drain()

    def _drain(self) -> None:
        """Read whatever the server has said (it only answers with coaching cues or errors). A reading it rejects is
        counted, so a wrong contract or an old server is reported instead of silently losing data."""
        for _ in range(50):
            try:
                reply = json.loads(self._ws.recv(timeout=0))
            except TimeoutError:
                return
            if reply.get("type") == "error":
                self.rejected += 1
                self.last_error = f"{reply.get('code')}: {reply.get('detail')}"
                if self.rejected >= 5 and self.rejected >= self.sent:
                    raise ServerRejected(f"the server is rejecting these readings ({self.last_error}). Is the server up to date with the hardware contract?")

    def finish(self) -> dict[str, Any] | None:
        """Close the socket and finish the workout so the readings are flushed. Returns the server's summary, or None."""
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass
        if not self.session_id or not self.token:
            return None
        try:
            return self._http("POST", f"{self.api}/api/workouts/{self.session_id}/finish", None, {"Authorization": f"Bearer {self.token}"})
        except RuntimeError:
            return None


def _ws_connect(url: str) -> Any:
    try:
        from websockets.sync.client import connect
    except ImportError:
        raise SystemExit("--upload needs the websockets package. Install it with:  pip install websockets") from None
    return connect(url, open_timeout=10)

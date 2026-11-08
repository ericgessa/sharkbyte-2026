"""The laptop receiver's upload path (hardware/laptop/grit_upload.py): base-station lines -> `sensor_update`.

Everything is faked (no serial port, no network). The server side of the same messages is covered by test_sensors.py,
test_form_iq.py and the live scripts/e2e_spine.py.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.schemas.sensors import SensorUpdate

_PATH = Path(__file__).resolve().parents[2] / "hardware" / "laptop" / "grit_upload.py"
_spec = importlib.util.spec_from_file_location("grit_upload", _PATH)
grit_upload = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grit_upload)


def line(**over) -> dict:
    return {"type": "data", "seq": 12, "t": 4567, "phase": "live", "torso": 12.3, "upper": 1.2, "mid": -0.4, "lumbar": 5.6, "sensors": {"C": 1.0}, "ok": 15, "lost": 0, **over}


def test_a_live_line_becomes_the_servers_fields():
    assert grit_upload.line_to_sample(line()) == (4567, {"imu_pitch_deg": 12.3, "spine_upper_deg": 1.2, "spine_mid_deg": -0.4, "spine_lumbar_deg": 5.6})


@pytest.mark.parametrize("phase", ["neutral", "bend_wait", "bend_hold"])
def test_calibrating_lines_are_not_sent(phase):
    assert grit_upload.line_to_sample(line(phase=phase)) is None


def test_status_lines_and_unusable_numbers_are_dropped():
    assert grit_upload.line_to_sample({"type": "status", "msg": "paired"}) is None
    assert grit_upload.line_to_sample(line(upper=None, mid=None)) == (4567, {"imu_pitch_deg": 12.3, "spine_lumbar_deg": 5.6})
    assert grit_upload.line_to_sample(line(lumbar=500)) == (4567, {"imu_pitch_deg": 12.3, "spine_upper_deg": 1.2, "spine_mid_deg": -0.4})
    assert grit_upload.line_to_sample(line(torso=None, upper=None, mid=None, lumbar=None)) is None
    assert grit_upload.line_to_sample(line(t=-1)) is None
    assert grit_upload.line_to_sample(line(torso=float("nan"), upper=None, mid=None, lumbar=None)) is None


def test_every_field_it_sends_is_one_the_server_accepts():
    t, data = grit_upload.line_to_sample(line())
    SensorUpdate.model_validate({"schema_version": 1, "type": "sensor_update", "session_id": "00000000-0000-4000-8000-000000000000", "device_id": "esp32-spine", "sequence": 0, "timestamp_ms": t, "data": data})


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self.fail_next = 0
        self.replies: list[str] = []  # what the "server" will say next

    def send(self, text: str) -> None:
        if self.fail_next:
            self.fail_next -= 1
            raise OSError("connection reset")
        self.sent.append(json.loads(text))

    def recv(self, timeout=None) -> str:
        if not self.replies and timeout is None:
            return json.dumps({"type": "session_ready"})
        if not self.replies:
            raise TimeoutError
        return self.replies.pop(0)

    def close(self) -> None:
        self.closed = True


def make_uploader(monkeypatch, sockets: list[FakeWS]):
    monkeypatch.setenv("GRIT_TOKEN", "tok-1")
    calls: list[tuple[str, str]] = []
    up = grit_upload.Uploader("http://api.test/", "squat", connect=lambda url: sockets.pop(0))

    def http(method, url, body=None, headers=None):
        calls.append((method, url))
        if url.endswith("/api/workouts") and method == "POST":
            return {"session_id": "sess-1"}
        if url.endswith("/finish"):
            return {"status": "completed", "total_reps": 0}
        raise AssertionError(url)

    up._http = http
    return up, calls


def test_it_starts_a_workout_authenticates_and_sends_numbered_updates(monkeypatch):
    ws = FakeWS()
    up, calls = make_uploader(monkeypatch, [ws])
    assert up.start() == "sess-1"
    assert calls == [("POST", "http://api.test/api/workouts")]
    assert ws.sent[0] == {"schema_version": 1, "type": "auth", "access_token": "tok-1"}
    for t in (1000, 1050, 1100):
        assert up.add(line(t=t)) is True
    updates = [m for m in ws.sent if m["type"] == "sensor_update"]
    assert [u["sequence"] for u in updates] == [0, 1, 2]
    assert updates[0]["session_id"] == "sess-1" and updates[0]["device_id"] == "esp32-spine" and updates[0]["timestamp_ms"] == 1000
    assert up.sent == 3


def test_it_caps_the_rate_and_skips_non_samples(monkeypatch):
    ws = FakeWS()
    up, _ = make_uploader(monkeypatch, [ws])
    up.start()
    assert up.add(line(t=1000)) is True
    assert up.add(line(t=1010)) is False  # faster than 25 per second
    assert up.add(line(t=1100, phase="neutral")) is False
    assert up.add({"type": "status", "msg": "x"}) is False
    assert up.add(line(t=1100)) is True
    assert up.sent == 2


def test_a_wearable_reboot_resets_its_clock_without_blocking_the_stream(monkeypatch):
    ws = FakeWS()
    up, _ = make_uploader(monkeypatch, [ws])
    up.start()
    up.add(line(t=90_000))
    assert up.add(line(t=500)) is True  # the ESP32's uptime restarted


def test_a_dropped_connection_reopens_and_the_reading_is_not_lost(monkeypatch):
    first, second = FakeWS(), FakeWS()
    up, _ = make_uploader(monkeypatch, [first, second])
    up.start()
    first.fail_next = 1
    assert up.add(line(t=1000)) is True
    assert [m["type"] for m in second.sent] == ["auth", "sensor_update"]  # reopened, re-authenticated, resent


def test_if_the_server_stays_unreachable_it_says_so_instead_of_crashing_silently(monkeypatch):
    first = FakeWS()
    up, _ = make_uploader(monkeypatch, [first])
    up.start()
    first.fail_next = 5
    with pytest.raises(RuntimeError, match="lost the connection"):
        up.add(line(t=1000))


def test_finishing_closes_the_socket_and_the_workout(monkeypatch):
    ws = FakeWS()
    up, calls = make_uploader(monkeypatch, [ws])
    up.start()
    assert up.finish() == {"status": "completed", "total_reps": 0}
    assert ws.closed and calls[-1] == ("POST", "http://api.test/api/workouts/sess-1/finish")


def test_an_existing_workout_can_be_used_instead_of_starting_one(monkeypatch):
    ws = FakeWS()
    up, calls = make_uploader(monkeypatch, [ws])
    assert up.start("existing-id") == "existing-id"
    assert calls == []  # nothing created


def test_the_password_is_never_put_in_the_source_or_on_a_command_line():
    text = _PATH.read_text(encoding="utf-8")
    assert "getpass" in text and "GRIT_PASSWORD" in text
    assert "--password" not in text


def test_a_server_that_rejects_every_reading_is_reported_not_ignored(monkeypatch):
    ws = FakeWS()
    up, _ = make_uploader(monkeypatch, [ws])
    up.start()
    ws.replies = [json.dumps({"type": "error", "code": "invalid_message", "detail": "invalid fields: sensor_update.data.spine_lumbar_deg"})] * 10
    with pytest.raises(RuntimeError, match="rejecting these readings"):
        for i in range(10):
            up.add(line(t=1000 + i * 50))
    assert "invalid_message" in up.last_error


def test_coaching_cues_from_the_server_are_not_counted_as_rejections(monkeypatch):
    ws = FakeWS()
    up, _ = make_uploader(monkeypatch, [ws])
    up.start()
    ws.replies = [json.dumps({"type": "coaching_feedback", "message": "x"})] * 3
    for i in range(3):
        up.add(line(t=1000 + i * 50))
    assert up.rejected == 0

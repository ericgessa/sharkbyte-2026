"""hardware/laptop/spine_bridge.py: its parsing, and the real script driving a real (uvicorn) server end to end.

The serial port is faked (lines are fed in by the test); no hardware is involved. Readings are made up.
"""

import argparse
import asyncio
import importlib.util
import json
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
import websockets

from app.main import create_app
from tests.fakes import ALICE, FakeAuth, FakeTiger, InMemoryStore, auth_message

SCRIPT = Path(__file__).resolve().parents[2] / "hardware" / "laptop" / "spine_bridge.py"
pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="hardware/ folder not present")


def load_bridge():
    spec = importlib.util.spec_from_file_location("spine_bridge", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bridge = load_bridge() if SCRIPT.exists() else None

LIVE = '{"t":4567,"imu_pitch_deg":12.3,"imu_roll_deg":-2.0,"spine_upper_deg":1.2,"spine_mid_deg":-0.4,"spine_lumbar_deg":5.6,"seq":12,"phase":"live","ok":15,"lost":0}'


# ---- pure parts -----------------------------------------------------------------


def test_parses_base_station_lines_like_the_app_does():
    assert bridge.parse_sample(LIVE + "\r\n") == (
        4567,
        {"imu_pitch_deg": 12.3, "imu_roll_deg": -2.0, "spine_upper_deg": 1.2, "spine_mid_deg": -0.4, "spine_lumbar_deg": 5.6},
    )
    assert bridge.parse_sample('{"t":1,"spine_lumbar_deg":181,"spine_mid_deg":"3","imu_pitch_deg":20,"x":1}') == (1, {"imu_pitch_deg": 20.0})
    for line in ['{"t":10,"seq":2,"phase":"neutral","ok":15,"lost":0}', '{"type":"status","msg":"paired"}', "{half", "", "torso 1.0", '{"t":-1,"imu_pitch_deg":1}', '{"t":true,"imu_pitch_deg":1}']:
        assert bridge.parse_sample(line) is None, line


def test_status_and_calibration_lines_become_notes():
    assert bridge.note_for('{"type":"status","msg":"paired with wearable AA:BB"}') == "base station: paired with wearable AA:BB"
    assert bridge.note_for('{"t":10,"phase":"neutral","ok":15}').startswith("wearable calibrating")
    assert bridge.note_for(LIVE) is None
    assert bridge.note_for("  ") is None


def test_live_lines_without_usable_angles_explain_why_instead_of_going_silent():
    assert "re-flash" in bridge.note_for('{"t":5,"torso":12.3,"upper":1.2,"mid":-0.4,"lumbar":5.6,"phase":"live","ok":15}')
    assert "no sensor is working" in bridge.note_for('{"t":5,"seq":3,"phase":"live","ok":0,"lost":0}')
    assert "--raw" in bridge.note_for('{"t":5,"seq":3,"phase":"live","ok":15,"lost":0}')


def test_server_address_becomes_the_socket_address():
    assert bridge.to_ws_url("http://192.168.1.20:8000/") == "ws://192.168.1.20:8000"
    assert bridge.to_ws_url("https://api.example.com") == "wss://api.example.com"
    assert bridge.to_ws_url("localhost:8000") == "ws://localhost:8000"


def test_server_addresses_are_cleaned_up_or_rejected():
    assert bridge.normalize_server("192.168.1.20:8000") == "http://192.168.1.20:8000"
    assert bridge.normalize_server(' "http://192.168.1.20:8000/" ') == "http://192.168.1.20:8000"
    assert bridge.normalize_server("wss://api.example.com") == "https://api.example.com"
    for bad in ("", "   ", "ftp://x", "http://", "http://x:notaport"):
        assert bridge.normalize_server(bad) is None, bad


def test_whatever_the_user_pastes_yields_the_server_and_the_code():
    cmd = "python hardware/laptop/spine_bridge.py --server http://192.168.1.20:8000 --code ABCD-2345"
    assert bridge.parse_pairing_input(cmd) == ("http://192.168.1.20:8000", "ABCD-2345")
    assert bridge.parse_pairing_input("abcd-2345") == (None, "abcd-2345")
    assert bridge.parse_pairing_input("  ABCD2345 ") == (None, "ABCD2345")
    assert bridge.parse_pairing_input("http://my-host-name:8000 WXYZ-6789") == ("http://my-host-name:8000", "WXYZ-6789")
    assert bridge.parse_pairing_input("10.0.0.5:8000") == ("http://10.0.0.5:8000", None)
    assert bridge.parse_pairing_input("python hardware/laptop/spine_bridge.py") == (None, None)  # 'hardware' is no code
    assert bridge.parse_pairing_input("") == (None, None)


def test_each_way_of_failing_to_connect_gets_its_own_explanation():
    refused = ConnectionRefusedError(10061, "refused")
    msg, ask_again = bridge.explain_unreachable("http://localhost:8000", refused)
    assert "THIS laptop" in msg and ask_again  # localhost on the bridge laptop: the address is wrong, ask again
    msg, ask_again = bridge.explain_unreachable("http://192.168.1.20:8000", refused)
    assert "dev_server" in msg and "0.0.0.0" in msg and not ask_again  # server down or local-only: keep retrying
    msg, ask_again = bridge.explain_unreachable("http://192.168.1.20:8000", TimeoutError("timed out"))
    assert "firewall" in msg.lower() and not ask_again
    msg, ask_again = bridge.explain_unreachable("http://nosuchhost:8000", socket.gaierror(11001, "getaddrinfo failed"))
    assert "nosuchhost" in msg and ask_again


def test_check_server_knows_the_real_api_and_a_closed_port(server):
    base, _ = server
    assert bridge.check_server(base) == (None, False)
    closed = f"http://127.0.0.1:{free_port()}"
    problem, ask_again = bridge.check_server(closed, timeout=2)
    assert problem and "THIS laptop" in problem and ask_again


def test_a_rejected_code_is_dropped_and_the_user_asked_again_never_retried(server, monkeypatch):
    base, _ = server
    attempts = []
    real_redeem = bridge_app_state["app"].state.sensor_pairing.redeem

    def counting_redeem(code, client=""):
        attempts.append(code)
        return real_redeem(code, client)

    monkeypatch.setattr(bridge_app_state["app"].state.sensor_pairing, "redeem", counting_redeem)

    async def scenario():
        asked = asyncio.Event()

        async def fake_ask(prompt):
            asked.set()
            await asyncio.Event().wait()  # the user hasn't typed the new code yet

        monkeypatch.setattr(bridge, "ask", fake_ask)
        b = bridge.Bridge(argparse.Namespace(server=base, code="WRNG-CODE", raw=False), FakeLink())
        run = asyncio.create_task(b.run())
        await asyncio.wait_for(asked.wait(), 5)
        await asyncio.sleep(0.3)  # time for any (wrong) retry loop to show itself
        run.cancel()
        assert b.code is None and b.token is None

    asyncio.run(scenario())
    assert attempts == ["WRNG-CODE"]  # tried once: a retry could never succeed and would count as guessing


def test_streamer_numbers_messages_and_caps_the_rate_by_device_clock():
    s = bridge.Streamer("sid", max_hz=25)
    a, b, c = s.next(1000, {"spine_mid_deg": 1.0}), s.next(1010, {"spine_mid_deg": 1.0}), s.next(1040, {"spine_mid_deg": 2.0})
    assert a["sequence"] == 0 and b is None and c["sequence"] == 1
    assert s.next(5, {"spine_mid_deg": 1.0})["sequence"] == 2  # ESP32 reboot: clock went back, accepted


# ---- the real script against a real server -----------------------------------------


class FakeLink:
    def __init__(self):
        self.lines: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[bytes] = []

    def send(self, command: bytes) -> None:
        self.sent.append(command)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


bridge_app_state: dict = {}  # the running test server's app, for tests that look inside it


@pytest.fixture
def server():
    tiger = FakeTiger()
    app = create_app(store=InMemoryStore(), auth=FakeAuth(), timeseries=tiger)
    bridge_app_state["app"] = app
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    end = time.time() + 10
    while not srv.started and time.time() < end:
        time.sleep(0.02)
    assert srv.started
    yield f"http://127.0.0.1:{port}", tiger
    srv.should_exit = True
    thread.join(5)


async def recv_type(ws, wanted: str, timeout=5.0) -> dict:
    async def loop():
        while True:
            m = json.loads(await ws.recv())
            if m["type"] == wanted:
                return m
    return await asyncio.wait_for(loop(), timeout)


async def until(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


def test_the_bridge_script_pairs_streams_relays_recalibrate_and_lets_go_when_the_workout_ends(server):
    base, tiger = server
    http = httpx.Client(base_url=base)
    sid = http.post("/api/workouts", json={"exercise": "squat"}, headers=ALICE).json()["session_id"]
    code = http.post(f"/api/workouts/{sid}/sensor-code", headers=ALICE).json()["code"]

    async def scenario():
        link = FakeLink()
        args = argparse.Namespace(server=base, code=code.lower(), raw=False)
        b = bridge.Bridge(args, link)
        run = asyncio.create_task(b.run())
        async with websockets.connect(f"{bridge.to_ws_url(base)}/ws/workouts/{sid}") as app_ws:
            await app_ws.send(json.dumps(auth_message()))
            await recv_type(app_ws, "session_ready")
            assert await until(lambda: b.ws is not None), "bridge never paired"
            assert b.session_id == sid

            # calibration first, then live data
            await link.lines.put('{"t":100,"seq":1,"phase":"neutral","ok":15,"lost":0}\n')
            note = await recv_type(app_ws, "sensor_link")
            while not (note.get("note") or "").startswith("wearable calibrating"):
                note = await recv_type(app_ws, "sensor_link")
            for i in range(10):
                await link.lines.put(LIVE.replace('"t":4567', f'"t":{5000 + 50 * i}') + "\n")
            live = await recv_type(app_ws, "sensor_live")
            assert live["data"]["spine_lumbar_deg"] == 5.6
            assert await until(lambda: len([r for r in tiger.rows if r.metric_name == "spine_lumbar"]) == 10)

            # Recalibrate pressed in the app reaches the wearable's serial port
            await app_ws.send(json.dumps({"schema_version": 1, "type": "device_command", "session_id": sid, "command": "recalibrate"}))
            assert await until(lambda: b"c" in link.sent)

            # The workout ends: the bridge drops its pairing and goes back to waiting for a code
            asked = asyncio.Event()

            async def fake_ask(prompt):
                asked.set()
                await asyncio.Event().wait()  # never answers

            bridge.ask = fake_ask
            assert http.post(f"/api/workouts/{sid}/finish", headers=ALICE).status_code == 200
            await link.lines.put(LIVE.replace('"t":4567', '"t":9000') + "\n")  # next send hits the closed workout
            await asyncio.wait_for(asked.wait(), 5)
            assert b.token is None and b.session_id is None
        run.cancel()
        assert link.sent[0] == b"s"  # put a USB wearable into stream mode at start

    asyncio.run(scenario())


# ---- finding the base station's port -------------------------------------------------


class FakePortInfo:
    def __init__(self, device, description="USB Serial Device", vid=None, manufacturer=None):
        self.device, self.description, self.vid, self.manufacturer = device, description, vid, manufacturer


class FakeSerialModule:
    """serial.Serial stand-in: each port replays scripted lines; 'busy' ports refuse to open."""

    class SerialException(Exception):
        pass

    def __init__(self, scripts, busy=()):
        self.scripts, self.busy, self.opened = scripts, set(busy), []

    def Serial(self, device, baud, timeout=1):  # noqa: N802 - mirrors pyserial
        if device in self.busy:
            raise self.SerialException("Access is denied")
        self.opened.append(device)
        lines = list(self.scripts.get(device, []))

        class S:
            def readline(self_inner):
                return (lines.pop(0) + "\n").encode() if lines else b""

            def close(self_inner):
                pass

        return S()


class FakeListPorts:
    def __init__(self, *rounds):
        self.rounds = list(rounds)

    def comports(self):
        return self.rounds.pop(0) if len(self.rounds) > 1 else self.rounds[0]


def test_lines_say_which_board_is_talking():
    assert bridge.identify('{"type":"status","msg":"base station ready"}') == "base"
    assert bridge.identify(LIVE) == "base"  # data lines from the base station carry "seq"
    assert bridge.identify('{"t":5,"spine_lumbar_deg":3}') == "wired"  # spine_test stream over USB
    assert bridge.identify("=== Spine test: 3x MPU-6050 + BNO085 ===") == "wired"
    assert bridge.identify("[radio] searching for the base station") == "wireless"
    assert bridge.identify("hello") is None


def test_only_esp32_looking_ports_are_candidates_known_chips_first():
    ports = [FakePortInfo("COM1", "Communications Port"), FakePortInfo("COM7", "USB Serial Device"), FakePortInfo("COM10", "USB Serial Device", vid=0x303A)]
    assert [p.device for p in bridge.candidate_ports(ports)] == ["COM10", "COM7"]


def test_the_base_station_wins_then_a_silent_board_then_the_wireless_wearable():
    assert bridge.pick({"COM3": "wireless", "COM10": "base"}) == "COM10"
    assert bridge.pick({"COM3": "wireless", "COM10": "silent"}) == "COM10"  # an unpaired base station says nothing
    assert bridge.pick({"COM3": "busy", "COM10": "wireless"}) == "COM10"
    assert bridge.pick({"COM3": "busy"}) is None


def test_with_two_esp32s_plugged_in_it_listens_and_picks_the_base_station(monkeypatch):
    monkeypatch.setattr(bridge, "PROBE_SECONDS", 0.3)
    ports = FakeListPorts([FakePortInfo("COM3", vid=0x303A), FakePortInfo("COM10", vid=0x303A)])
    serial_mod = FakeSerialModule({"COM3": ["[radio] paired with base AA:BB"], "COM10": ['{"type":"status","msg":"paired with wearable"}']})
    assert bridge.find_port(serial_mod, ports, verbose=False) == "COM10"


def test_it_waits_for_the_base_station_to_be_plugged_in_and_skips_a_busy_port(monkeypatch):
    monkeypatch.setattr(bridge, "RESCAN_SECONDS", 0.01)
    ports = FakeListPorts([], [], [FakePortInfo("COM10", vid=0x303A)])  # nothing, nothing, then plugged in
    ser, port = bridge.open_base_station(FakeSerialModule({}), ports, None, 115200)
    assert port == "COM10"
    # --port pointing at a port that's gone/busy falls back to scanning
    ser, port = bridge.open_base_station(FakeSerialModule({}, busy={"COM5"}), FakeListPorts([FakePortInfo("COM10", vid=0x303A)]), "COM5", 115200)
    assert port == "COM10"


def test_after_an_unplug_the_bridge_finds_the_base_station_again_and_keeps_reading(monkeypatch):
    class Link(FakeLink):
        replaced = None

        def replace(self, ser):
            self.replaced = ser

    async def scenario():
        link = Link()
        b = bridge.Bridge(argparse.Namespace(server="http://x", code=None, raw=False, baud=115200), link, object(), object(), "COM10")
        monkeypatch.setattr(bridge, "open_base_station", lambda *a: ("NEW-SERIAL", "COM11"))
        task = asyncio.create_task(b.read_serial())
        await link.lines.put("__serial_error__ device disconnected")
        assert await until(lambda: link.replaced == "NEW-SERIAL")
        assert b.port == "COM11" and link.sent.count(b"s") == 2  # stream mode asked for again after the replug
        await link.lines.put('{"type":"status","msg":"paired with wearable"}\n')  # still reading afterwards
        assert await until(lambda: b.last_note == "base station: paired with wearable")
        task.cancel()

    asyncio.run(scenario())


def test_the_waiting_message_says_what_the_computer_can_see():
    assert "NO serial ports" in bridge.waiting_message([])
    msg = bridge.waiting_message([FakePortInfo("COM1", "Communications Port")])
    assert "COM1 (Communications Port)" in msg and "--port" in msg

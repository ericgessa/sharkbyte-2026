#!/usr/bin/env python3
"""
Spine bridge: reads the base station (or a USB wearable) on this laptop and streams it to the SharkByte
backend, which stores it and shows it live in the app. No browser needed, so it works with the app on a phone.

Setup (once):   pip install -r requirements.txt
Run:            python spine_bridge.py
                then paste the line the app shows under the pairing code, e.g.
                python hardware/laptop/spine_bridge.py --server http://192.168.1.20:8000 --code ABCD-2345
                (or run that line directly). The bridge finds the base station by itself and waits for it to be
                plugged in; --port COM5 forces a port. The last server that worked is remembered.

In the app: start a workout, press "Pair laptop bridge"; it shows the code AND the server address to use. The
bridge follows that workout until it finishes, then asks for the next code. Recalibrate from the app. Ctrl+C stops.
Your password never goes to this script: the code pairs it with one workout.

"localhost" means THIS laptop. If the server runs on another computer, use that computer's address (the app shows
it); the bridge checks the address first and says exactly what is wrong if it can't connect.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

# The server's limits (server/app/schemas/sensors.py). An out-of-range value would get the message rejected.
LIMITS = {
    "imu_pitch_deg": 360,
    "imu_roll_deg": 360,
    "spine_upper_deg": 180,
    "spine_mid_deg": 180,
    "spine_lumbar_deg": 180,
}
PHASES = {
    "neutral": "wearable calibrating: stand tall and hold still",
    "bend_wait": "wearable calibrating: slowly bend forward and hold",
    "bend_hold": "wearable calibrating: hold it...",
}
OLD_NAMES = ("torso", "upper", "mid", "lumbar")  # field names before the base station spoke the app's format
PORT_HINTS = ("cp210", "ch340", "ch910", "wch", "silicon labs", "usb serial", "usb jtag", "espressif", "esp32")
DEVICE_ID = "esp32-spine"
MAX_HZ = 25  # the firmware sends about 20/s; this only guards against a flood

# Server close codes (docs/event-contracts.md)
CLOSE_UNAUTHENTICATED, CLOSE_NOT_FOUND, CLOSE_NOT_ACTIVE, CLOSE_REPLACED = 4401, 4404, 4409, 4410


# ---------------------------------------------------------------- pure parts (tested in test_spine_bridge.py)


def parse_sample(line: str) -> tuple[int, dict[str, float]] | None:
    """A data line -> (device ms, angles). None for status lines, calibration lines, half lines and garbage.
    Same rules as the app's parser (mobile/src/pose/wearable.ts)."""
    s = line.strip()
    if not s.startswith("{"):
        return None
    try:
        o = json.loads(s)
    except json.JSONDecodeError:
        return None
    if not isinstance(o, dict):
        return None
    t = o.get("t")
    if not isinstance(t, int) or isinstance(t, bool) or t < 0:
        return None
    data = {}
    for name, limit in LIMITS.items():
        v = o.get(name)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and abs(v) <= limit:
            data[name] = float(v)
    return (t, data) if data else None


def note_for(line: str) -> str | None:
    """A non-data line -> a short note for the app (pairing, calibration), or None for nothing worth showing."""
    s = line.strip()
    if not s:
        return None
    if s.startswith("{"):
        try:
            o = json.loads(s)
        except json.JSONDecodeError:
            return None
        if not isinstance(o, dict):
            return None
        if isinstance(o.get("msg"), str):
            return f"base station: {o['msg']}"[:200]
        if isinstance(o.get("phase"), str) and o["phase"] != "live":
            return PHASES.get(o["phase"], f"wearable: {o['phase']}")
        if o.get("phase") == "live" and parse_sample(s) is None:  # calibrated, but nothing usable in the line
            if any(k in o for k in OLD_NAMES):
                return "base station sends the old field names (torso, lumbar...): re-flash firmware/base_station"
            if o.get("ok") == 0:
                return "wearable is live but no sensor is working (ok=0): check the I2C wiring and power"
            return "wearable is live but sends no angles (run the bridge with --raw to see the lines)"
        return None
    return s[:200]  # spine_test's plain-text prompts


def to_ws_url(server: str) -> str:
    """http://host:8000/ -> ws://host:8000 (https -> wss)."""
    s = server.strip().rstrip("/")
    if s.startswith("https://"):
        return "wss://" + s[len("https://"):]
    if s.startswith("http://"):
        return "ws://" + s[len("http://"):]
    return s if s.startswith(("ws://", "wss://")) else "ws://" + s


LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
URL_RE = re.compile(r"(?:https?|wss?)://[^\s'\"<>]+", re.IGNORECASE)
HEALTH_TIMEOUT_S = 4.0
MAX_BACKOFF_S = 10.0
CONFIG_PATH = Path.home() / ".sharkbyte_bridge.json"


def is_loopback(host: str | None) -> bool:
    h = (host or "").strip("[]").lower()
    return h in LOOPBACK_HOSTS or h.startswith("127.")


def normalize_server(text: str | None) -> str | None:
    """'192.168.1.20:8000', 'http://x:8000/', '"http://x:8000"' -> 'http://x:8000'. None if it isn't an address."""
    s = (text or "").strip().strip("'\"")
    if not s:
        return None
    if "://" not in s:
        s = "http://" + s  # the result is rebuilt from scheme + host:port, so a trailing slash or path drops off
    parts = urlsplit(s)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme.lower(), parts.scheme.lower())
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    try:
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        return None
    return f"{scheme}://{parts.netloc}"


def parse_pairing_input(text: str) -> tuple[str | None, str | None]:
    """What the user typed or pasted -> (server address or None, pairing code or None).

    Accepts a bare code ('abcd-2345'), a bare address, or the whole command line the app shows
    ('python ... --server http://192.168.1.20:8000 --code ABCD-2345')."""
    s = (text or "").strip()
    server = None
    m = re.search(r"--server[ =]+(\S+)", s)
    if m:
        server = normalize_server(m.group(1))
    else:
        u = URL_RE.search(s)
        if u:
            server = normalize_server(u.group(0))
    rest = re.sub(r"--server[ =]+\S+", " ", URL_RE.sub(" ", s))  # host names can contain dashes: look elsewhere
    code = None
    m = re.search(r"--code[ =]+(\S+)", rest)
    if m:
        code = m.group(1)
    else:
        m = re.search(r"(?<![A-Za-z0-9])([A-Za-z0-9]{4}-[A-Za-z0-9]{4})(?![A-Za-z0-9])", rest)
        if m:
            code = m.group(1)
        else:
            words = rest.split()
            if len(words) == 1 and len(re.sub(r"[^A-Za-z0-9]", "", words[0])) == 8:
                code = words[0]
            elif server is None and len(words) == 1:
                server = normalize_server(words[0]) if ("." in words[0] or ":" in words[0]) else None
    return server, code


def explain_unreachable(server: str, reason: object, timeout: float = HEALTH_TIMEOUT_S) -> tuple[str, bool]:
    """Why ``server`` can't be reached, in plain words -> (message, ask for a different address?)."""
    parts = urlsplit(server)
    host = parts.hostname or "?"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    text = str(reason).lower()
    winerror = getattr(reason, "winerror", None)
    refused = isinstance(reason, ConnectionRefusedError) or winerror in (10061, 1225) or "refused" in text
    timed_out = isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in text or winerror == 10060
    # No firewall stands between a computer and itself, and Windows takes ~2 s to refuse a closed local port: a
    # localhost timeout means the same as a refusal, nothing is running here.
    if refused or (timed_out and is_loopback(host)):
        if is_loopback(host):
            return (
                f"Nothing is running at {host}:{port} on THIS laptop. 'localhost' means this computer, not the one\n"
                "running the server. Use the server's address shown in the app under the pairing code\n"
                "(for example http://192.168.1.20:8000).",
                True,
            )
        return (
            f"{host} refused port {port}. Either the server isn't running there, or it only accepts connections from\n"
            "its own computer. On the server computer start it with:  python -m scripts.dev_server   (from server/)\n"
            "(plain 'uvicorn app.main:app' needs --host 0.0.0.0). Also check the port: the API is usually 8000;\n"
            "8081/8080 is the app's web page, not the API. Retrying...",
            False,
        )
    if timed_out:
        return (
            f"No answer from {host}:{port} after {timeout:.0f} s. Usually the server computer's firewall is blocking\n"
            f"port {port}: on Windows allow Python through 'Windows Defender Firewall -> Allow an app' for Private\n"
            "networks (and make sure the Wi-Fi is set to Private). Or the two computers are on different networks:\n"
            "guest/campus Wi-Fi often blocks computer-to-computer traffic (a phone hotspot works). Retrying...",
            False,
        )
    if isinstance(reason, socket.gaierror) or "getaddrinfo" in text or "name or service" in text:
        return (f"Can't find a computer called '{host}'. Check the address the app shows.", True)
    if winerror in (10051, 10065) or "unreachable" in text:
        return (f"{host} is on a network this laptop can't reach. Are both computers on the same Wi-Fi? Retrying...", False)
    return (f"Can't reach {server} ({reason}). Retrying...", False)


def check_server(server: str, timeout: float = HEALTH_TIMEOUT_S) -> tuple[str | None, bool]:
    """Ask the server's /health -> (None, False) if it is the SharkByte API; else (what's wrong, ask for a new address?)."""
    try:
        with urllib.request.urlopen(f"{server}/health", timeout=timeout) as r:
            body = json.loads(r.read().decode() or "{}")
        if isinstance(body, dict) and body.get("status") == "ok":
            return None, False
        return (f"{server} answered, but it isn't the SharkByte API (wrong port? the API is usually 8000).", True)
    except urllib.error.HTTPError as e:
        return (f"{server} answered HTTP {e.code} on /health: that isn't the SharkByte API (wrong port? the API is "
                "usually 8000; 8081/8080 is the app's web page).", True)
    except urllib.error.URLError as e:
        return explain_unreachable(server, e.reason, timeout)
    except (OSError, ValueError) as e:  # timeouts can arrive unwrapped; ValueError = not JSON
        if isinstance(e, ValueError) and not isinstance(e, OSError):
            return (f"{server} answered, but it isn't the SharkByte API (wrong port? the API is usually 8000).", True)
        return explain_unreachable(server, e, timeout)


def load_saved_server(path: Path = CONFIG_PATH) -> str | None:
    try:
        return normalize_server(json.loads(path.read_text()).get("server"))
    except Exception:
        return None


def save_server(server: str, path: Path = CONFIG_PATH) -> None:
    try:
        path.write_text(json.dumps({"server": server}))
    except OSError:
        pass  # remembering is a convenience only


class Streamer:
    """Samples -> numbered sensor_update messages, capped by the device clock (like the app's SensorStreamer)."""

    def __init__(self, session_id: str, max_hz: float = MAX_HZ) -> None:
        self.session_id = session_id
        self.min_interval_ms = 1000 / max_hz
        self.sequence = 0
        self.last_ms = -math.inf

    def next(self, t: int, data: dict[str, float]) -> dict | None:
        gap = t - self.last_ms
        if 0 <= gap < self.min_interval_ms:
            return None
        self.last_ms = t  # gap < 0: the ESP32 rebooted; the server re-anchors its clock
        msg = {"schema_version": 1, "type": "sensor_update", "session_id": self.session_id, "device_id": DEVICE_ID,
               "sequence": self.sequence, "timestamp_ms": t, "data": data}
        self.sequence += 1
        return msg


# ---------------------------------------------------------------- serial port


# USB vendor ids of the chips ESP32 boards use for their USB port.
ESP_VIDS = {0x303A: "Espressif (native USB)", 0x10C4: "Silicon Labs CP210x", 0x1A86: "WCH CH340", 0x0403: "FTDI"}
# What a board turned out to be after listening to it, best first.
PREFERENCE = ["base", "wired", "silent", "wireless", "other"]
PROBE_SECONDS = 2.5
RESCAN_SECONDS = 2.0


def candidate_ports(ports) -> list:
    """Ports that look like an ESP32, most likely first: a known USB chip and a matching description."""

    def score(p) -> int:
        s = 2 if getattr(p, "vid", None) in ESP_VIDS else 0
        if any(h in f"{p.description} {p.manufacturer or ''}".lower() for h in PORT_HINTS):
            s += 1
        return s

    return sorted((p for p in ports if score(p) > 0), key=score, reverse=True)


def identify(line: str) -> str | None:
    """What kind of board printed this line: 'base' (base station), 'wired' (spine_test over USB),
    'wireless' (the wireless wearable's own USB debug output), or None if it doesn't say."""
    s = line.strip()
    if s.startswith("{"):
        try:
            o = json.loads(s)
        except json.JSONDecodeError:
            return None
        if not isinstance(o, dict):
            return None
        if o.get("type") == "status" or "seq" in o:
            return "base"
        return "wired" if "t" in o else None
    if "[radio]" in s or s.startswith("=== Spine wearable"):
        return "wireless"
    if s.startswith("=== Spine test"):
        return "wired"
    return None


def pick(found: dict[str, str]) -> str | None:
    """{port: kind} -> the port to use. The base station wins; a silent ESP32 beats the wireless wearable's
    own USB port (a base station with no wearable paired says nothing); a port another program holds can't win."""
    usable = {p: k for p, k in found.items() if k != "busy"}
    for kind in PREFERENCE:
        for port, k in usable.items():
            if k == kind:
                return port
    return None


def probe(serial, device: str, seconds: float = PROBE_SECONDS) -> str:
    """Listen to one port for a moment (sends nothing) and say what's on it."""
    try:
        ser = serial.Serial(device, 115200, timeout=0.3)
    except Exception:
        return "busy"
    kind = None
    try:
        end = time.monotonic() + seconds
        heard = False
        while time.monotonic() < end and kind != "base":
            line = ser.readline().decode(errors="ignore")
            if line.strip():
                heard = True
                kind = identify(line) or kind
        return kind or ("other" if heard else "silent")
    finally:
        ser.close()


def find_port(serial, list_ports, verbose: bool = True) -> str | None:
    """The base station's port, or None if no ESP32 is plugged in. Several candidates are listened to at once."""
    ports = candidate_ports(list_ports.comports())
    if not ports:
        return None
    if len(ports) == 1:
        return ports[0].device
    found: dict[str, str] = {}
    threads = [threading.Thread(target=lambda d=p.device: found.__setitem__(d, probe(serial, d)), daemon=True) for p in ports]
    for t in threads:
        t.start()
    for t in threads:
        t.join(PROBE_SECONDS + 2)
    port = pick(found)
    if verbose:
        print("ESP32 ports found: " + ", ".join(f"{d} ({k})" for d, k in found.items()) + (f" -> using {port}" if port else ""))
    return port


def open_base_station(serial, list_ports, preferred: str | None, baud: int):
    """Blocks until the base station's port is open: tries `preferred` first (--port, or the port we just lost),
    then scans every couple of seconds. Returns (serial object, port name)."""
    waiting_said = 0.0  # when we last explained why we're waiting (0 = not yet); repeated every 30 s
    busy_said = False
    while True:
        for port in [p for p in (preferred, find_port(serial, list_ports, verbose=not waiting_said)) if p]:
            try:
                return serial.Serial(port, baud, timeout=1), port
            except serial.SerialException as e:
                if not busy_said:
                    print(f"\nCouldn't open {port}: {e}\n"
                          "Another program has it open: close the Arduino Serial Monitor, spine_receiver.py, "
                          "or a browser tab connected to the wearable. Retrying...")
                    busy_said = True
        if not waiting_said or time.monotonic() - waiting_said >= 30:
            print("\n" + waiting_message(list(list_ports.comports())))
            waiting_said = time.monotonic()
        time.sleep(RESCAN_SECONDS)


def waiting_message(ports) -> str:
    """Why we're still waiting, in terms of what the computer can actually see."""
    if not ports:
        return ("Waiting for the base station: this computer sees NO serial ports at all, so the board isn't connected\n"
                "as far as Windows is concerned. Check: the USB cable carries data (charge-only cables don't), try another\n"
                "USB port or cable, press the board's RESET button. When it shows up in Device Manager under\n"
                "'Ports (COM & LPT)', the bridge connects by itself (checking every 2 s)...")
    listed = ", ".join(f"{p.device} ({p.description})" for p in ports)
    return (f"Waiting for the base station. Ports seen: {listed}. None looks like an ESP32. If one of them IS the base\n"
            "station, start the bridge with --port COMx. Checking every 2 s...")


class SerialLink:
    """Reads lines on a background thread and hands them to the asyncio loop."""

    def __init__(self, ser, loop: asyncio.AbstractEventLoop) -> None:
        self.ser = ser
        self.loop = loop
        self.lines: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self.lock = threading.Lock()
        threading.Thread(target=self._read, args=(ser,), daemon=True).start()

    def _read(self, ser) -> None:
        while True:
            try:
                raw = ser.readline()
            except Exception as e:  # unplugged
                self.loop.call_soon_threadsafe(self._put, f"__serial_error__ {e}")
                return
            if raw:
                self.loop.call_soon_threadsafe(self._put, raw.decode(errors="ignore"))

    def replace(self, ser) -> None:
        """After a replug: read from the newly opened port (same queue, so nothing else changes)."""
        with self.lock:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = ser
        threading.Thread(target=self._read, args=(ser,), daemon=True).start()

    def _put(self, line: str) -> None:
        if self.lines.full():
            self.lines.get_nowait()  # drop the oldest: live data, not a log
        self.lines.put_nowait(line)

    def send(self, command: bytes) -> None:
        with self.lock:
            self.ser.write(command)


# ---------------------------------------------------------------- server link


class Bridge:
    def __init__(self, args, link: SerialLink, serial_mod=None, list_ports=None, port: str | None = None) -> None:
        self.args = args
        self.link = link
        self.serial_mod, self.list_ports, self.port = serial_mod, list_ports, port  # to find the port again after a replug
        self.server: str | None = normalize_server(getattr(args, "server", None))
        self.server_explicit = bool(getattr(args, "server_explicit", self.server is not None))  # typed on the command line
        self.reachable = False  # /health answered at self.server
        self.remember = None  # callable(server) to save the last good address (main() sets it; tests don't)
        self.code: str | None = getattr(args, "code", None)
        self.token: str | None = None
        self.session_id: str | None = None
        self.streamer: Streamer | None = None
        self.ws = None
        self.last_note: str | None = None
        self.calibrating = False
        self.hint_shown = False  # the "not paired yet" message, shown once instead of a live line
        self.backoff_s = 1.0
        self.last_problem: tuple[str, float] | None = None  # (message, when shown): don't repeat it every retry

    def _say_problem(self, message: str) -> None:
        now = time.monotonic()
        if self.last_problem is None or self.last_problem[0] != message or now - self.last_problem[1] >= 30:
            print(f"\n{message}")
            self.last_problem = (message, now)

    async def _wait_backoff(self) -> None:
        await asyncio.sleep(self.backoff_s)
        self.backoff_s = min(self.backoff_s * 2, MAX_BACKOFF_S)

    def _take_input(self, text: str) -> None:
        """A typed/pasted line: may carry a new server address, a code, or both."""
        server, code = parse_pairing_input(text)
        if server and server != self.server:
            self.server, self.server_explicit, self.reachable = server, True, False
        if code:
            self.code = code

    async def run(self) -> None:
        """Until the serial port fails or another bridge takes over (or Ctrl+C)."""
        tasks = {asyncio.create_task(self.read_serial()), asyncio.create_task(self.server_loop())}
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            t.result()  # re-raise a crash instead of hiding it

    async def server_loop(self) -> None:
        """Server address -> reachable? -> code -> paired session; every failure says why and backs off (max 10 s)."""
        import websockets

        while True:
            # 1) An address. Never assumed: 'localhost' is only right when the server runs on this same laptop.
            if self.server is None:
                self._take_input(await ask(
                    "\nServer address (the app shows it under the pairing code, e.g. http://192.168.1.20:8000)\n"
                    "or paste the whole line the app shows: "
                ))
                if self.server is None:
                    print("That isn't an address. It looks like http://192.168.1.20:8000 (the app shows it).")
                continue

            # 2) Is the SharkByte API really there? Checked before pairing, so a wrong address or a blocked port is
            #    explained instead of failing as a vague 'connection refused' over and over.
            if not self.reachable:
                problem, ask_again = await in_daemon_thread(check_server, self.server)
                if problem:
                    self._say_problem(problem)
                    if ask_again:  # the address itself is wrong (not just 'not up yet'): ask for the right one
                        self.server, self.server_explicit = None, False
                    else:
                        await self._wait_backoff()
                    continue
                self.reachable, self.last_problem, self.backoff_s = True, None, 1.0
                print(f"\nServer found at {self.server}.")
                if self.remember:
                    self.remember(self.server)

            # 3) A code (or a token from an earlier pairing).
            if self.token is None and self.code is None:
                self._take_input(await ask("\nPairing code from the app (or paste the whole line the app shows): "))
                if self.code is None:
                    print("No code in that. It looks like ABCD-2345 (in the app: Pair laptop bridge).")
                continue  # re-check: the paste may have changed the server too

            # 4) Pair (or reconnect) and stream.
            try:
                async with websockets.connect(to_ws_url(self.server) + "/ws/sensors", open_timeout=10, max_size=65536) as ws:
                    if not await self.handshake(ws):
                        self.code = None
                        await self._wait_backoff()
                        continue
                    self.ws, self.backoff_s = ws, 1.0
                    try:
                        await self.receive(ws)
                    finally:
                        self.ws = None
                # a clean close by the server lands here too: treat it like a dropped connection
                if not self.closed_with(ws.close_code):
                    return
            except websockets.ConnectionClosed as e:
                self.ws = None
                if not self.closed_with(e.rcvd.code if e.rcvd else None):
                    return
            except (OSError, asyncio.TimeoutError, websockets.InvalidURI, websockets.InvalidHandshake) as e:
                self.ws, self.reachable = None, False  # re-diagnose with /health on the next round
                self._say_problem(f"Lost the server at {self.server} ({e}). Checking it again...")
            if self.token is not None:  # reconnecting with the token: don't hammer a server that keeps dropping us
                await self._wait_backoff()

    async def handshake(self, ws) -> bool:
        auth = {"schema_version": 1, "type": "sensor_auth"}
        auth.update({"bridge_token": self.token} if self.token else {"pair_code": self.code})
        await ws.send(json.dumps(auth))
        ready = json.loads(await ws.recv())
        if ready.get("type") != "sensor_ready":
            print(f"\nUnexpected reply from the server: {ready}")
            return False
        first = self.session_id != ready["session_id"]
        self.token, self.code, self.session_id = ready["bridge_token"], None, ready["session_id"]
        if first:
            self.streamer = Streamer(self.session_id)
            print(f"\nPaired with your {ready.get('exercise', '')} workout. Streaming to the server.")
        else:
            print("\nReconnected.")
        if self.last_note:
            await self.send_note(self.last_note)
        return True

    def closed_with(self, code: int | None) -> bool:
        """React to the server closing the link. False = stop the bridge."""
        if code == CLOSE_REPLACED:
            print("\nAnother bridge took over this workout. Stopping.")
            return False
        if code == CLOSE_UNAUTHENTICATED and self.session_id is None:
            # Never retry the same code: it can't start working, and every retry counts as a wrong guess.
            print("\nThat code didn't work (wrong, already used, or expired). Get a new one in the app.")
            self.code = None
        elif code in (CLOSE_NOT_ACTIVE, CLOSE_NOT_FOUND):
            print("\nThe workout ended. Start another one in the app to keep streaming.")
        elif code == CLOSE_UNAUTHENTICATED:
            print("\nThe server no longer knows this pairing (it may have restarted). Get a new code in the app.")
        else:
            print(f"\nLost the server connection (code {code}). Reconnecting...")
            return True  # keep the token and try again
        self.token = self.session_id = self.streamer = None
        self.hint_shown = True  # the code prompt comes next; don't print over it
        return True

    async def receive(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue  # never crash the bridge over one odd message
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "device_command" and msg.get("command") == "recalibrate":
                self.link.send(b"c")
                print("\n-> recalibration requested from the app")
            elif msg.get("type") == "error":
                print(f"\n[server] {msg.get('code')}: {msg.get('detail')}")

    async def send_note(self, note: str) -> None:
        ws = self.ws
        if ws is None or self.session_id is None:
            return
        try:
            await ws.send(json.dumps({"schema_version": 1, "type": "sensor_note", "session_id": self.session_id, "note": note}))
        except Exception:
            pass  # the receive loop handles the disconnect

    async def read_serial(self) -> None:
        self.link.send(b"s")  # a USB wearable (spine_test) needs stream mode; the base station ignores it
        while True:
            line = await self.link.lines.get()
            if line.startswith("__serial_error__"):
                # Unplugged (or the board reset). Find it again, maybe on a new COM number; the server pairing stays.
                print(f"\nLost the base station ({line.split(' ', 1)[1]}). Plug it back in; looking for it...")
                ser, port = await in_daemon_thread(open_base_station, self.serial_mod, self.list_ports, self.port, self.args.baud)
                self.port = port
                self.link.replace(ser)
                self.link.send(b"s")  # a USB wearable (spine_test) needs stream mode again
                print(f"Back on {port}.")
                continue
            if self.args.raw:
                print(line.rstrip())
            sample = parse_sample(line)
            if sample is not None:
                t, data = sample
                if self.calibrating:  # data with angles again: calibration finished
                    self.calibrating = False
                    self.last_note = "wearable live"
                    print("\nCalibrated. Live data in degrees (+ = bending forward):")
                    await self.send_note(self.last_note)
                if self.ws is not None:
                    self.hint_shown = False
                    if not self.args.raw:
                        show(data, True)
                elif not self.hint_shown:
                    # Not paired: don't redraw a live line, it would overwrite the code prompt and what is typed.
                    self.hint_shown = True
                    print("\nWearable data is arriving, but NOTHING is sent to the phone until this bridge is paired.\n"
                          "In the app: Start workout -> Pair laptop bridge. Type the code here and press Enter: ",
                          end="", flush=True)
                msg = self.streamer.next(t, data) if self.streamer else None
                if msg is not None and self.ws is not None:
                    try:
                        await self.ws.send(json.dumps(msg))
                    except Exception:
                        pass  # disconnected: the receive loop reconnects; this sample is just dropped
                continue
            if line.strip().startswith("=== Spine"):
                self.link.send(b"s")  # the USB wearable restarted
            note = note_for(line)
            if note and note != self.last_note:
                self.last_note = note
                self.calibrating = note.startswith("wearable calibrating") or self.calibrating
                print(f"\n{note}")
                await self.send_note(note)


async def in_daemon_thread(fn, *args):
    """Run a blocking function on a daemon thread (so a long wait never keeps Ctrl+C from exiting)."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def work() -> None:
        try:
            result = fn(*args)
            loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(result))
        except BaseException as e:  # noqa: BLE001 - handed to the awaiting coroutine
            loop.call_soon_threadsafe(lambda: fut.done() or fut.set_exception(e))

    threading.Thread(target=work, daemon=True).start()
    return await fut


async def ask(prompt: str) -> str:
    """input() on a daemon thread, so a pending prompt never keeps the program from exiting on Ctrl+C."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str | None] = loop.create_future()

    def work() -> None:
        try:
            value: str | None = input(prompt)
        except EOFError:
            value = None
        loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(value))

    threading.Thread(target=work, daemon=True).start()
    value = await fut
    if value is None:
        sys.exit("No input available for the pairing code: pass it with --code.")
    return value


def fmt(v: float | None) -> str:
    return "   -- " if v is None else f"{v:6.1f}"


def show(d: dict[str, float], sending: bool) -> None:
    print(
        f"\rtorso {fmt(d.get('imu_pitch_deg'))} | side {fmt(d.get('imu_roll_deg'))} | upper {fmt(d.get('spine_upper_deg'))}"
        f" | mid {fmt(d.get('spine_mid_deg'))} | lumbar {fmt(d.get('spine_lumbar_deg'))} | {'sending' if sending else 'not paired'}   ",
        end="",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Stream the spine wearable from this laptop to the SharkByte backend")
    parser.add_argument(
        "--server",
        help="the backend's address, as shown in the app under the pairing code (e.g. http://192.168.1.20:8000). "
        "Without it: $SHARKBYTE_SERVER, else the last address that worked, else you're asked.",
    )
    parser.add_argument("--port", help="serial port of the base station, e.g. COM5 or /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--code", help="pairing code from the app (otherwise you're asked for it)")
    parser.add_argument("--raw", action="store_true", help="print every raw line from the base station")
    args = parser.parse_args()
    if args.server and normalize_server(args.server) is None:
        sys.exit(f"--server {args.server!r} isn't an address. It looks like http://192.168.1.20:8000")
    args.server_explicit = bool(args.server or os.environ.get("SHARKBYTE_SERVER"))
    args.server = args.server or os.environ.get("SHARKBYTE_SERVER") or load_saved_server()

    try:
        import serial
        from serial.tools import list_ports
        import websockets  # noqa: F401
    except ImportError:
        sys.exit("Missing packages. Install them with:  pip install -r requirements.txt")

    try:
        ser, port = open_base_station(serial, list_ports, args.port, args.baud)  # waits until it's plugged in
    except KeyboardInterrupt:
        sys.exit("\nStopped.")
    where = args.server or "not set yet (you'll be asked)"
    print(f"Reading the wearable on {port}. Server: {where}{'' if args.server_explicit or not args.server else ' (remembered)'}")
    print("Ctrl+C stops.")
    state: dict = {}

    async def go() -> None:
        link = state["link"] = SerialLink(ser, asyncio.get_running_loop())
        bridge = Bridge(args, link, serial, list_ports, port)
        bridge.remember = save_server
        await bridge.run()

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        pass
    finally:
        link = state.get("link")
        try:
            (link.ser if link else ser).close()
        except Exception:
            pass
        print("\nStopped.")


if __name__ == "__main__":
    main()

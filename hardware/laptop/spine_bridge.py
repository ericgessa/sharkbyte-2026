#!/usr/bin/env python3
"""
Spine bridge: reads the base station (or a USB wearable) on this laptop and streams it to the SharkByte
backend, which stores it and shows it live in the app. No browser needed, so it works with the app on a phone.

Setup (once):   pip install -r requirements.txt
Run:            python spine_bridge.py --server http://192.168.1.20:8000
                (finds the base station by itself and waits for it to be plugged in; --port COM5 forces a port;
                 --code ABCD-2345 skips the prompt)

In the app: start a workout, press "Pair laptop bridge", and type the code it shows here. The bridge follows
that workout until it finishes, then asks for the next code. Recalibrate from the app. Ctrl+C stops.
Your password never goes to this script: the code pairs it with one workout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import threading
import time

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
        self.ws_url = to_ws_url(args.server) + "/ws/sensors"
        self.code: str | None = args.code
        self.token: str | None = None
        self.session_id: str | None = None
        self.streamer: Streamer | None = None
        self.ws = None
        self.last_note: str | None = None
        self.calibrating = False
        self.hint_shown = False  # the "not paired yet" message, shown once instead of a live line

    async def run(self) -> None:
        """Until the serial port fails or another bridge takes over (or Ctrl+C)."""
        tasks = {asyncio.create_task(self.read_serial()), asyncio.create_task(self.server_loop())}
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            t.result()  # re-raise a crash instead of hiding it

    async def server_loop(self) -> None:
        import websockets

        while True:
            if self.token is None and self.code is None:
                self.code = (await ask("\nPairing code (in the app: Pair laptop bridge): ")).strip()
                if not self.code:
                    continue
            try:
                async with websockets.connect(self.ws_url, open_timeout=10, max_size=65536) as ws:
                    if not await self.handshake(ws):
                        self.code = None
                        continue
                    self.ws = ws
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
                print(f"\nCan't reach the server at {self.args.server} ({e}). Retrying in 3 s...")
                await asyncio.sleep(3)

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
            print("\nThat code didn't work (wrong, already used, or expired). Get a new one in the app.")
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
            msg = json.loads(raw)
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
    parser.add_argument("--server", default="http://localhost:8000", help="the backend's address, as the app uses it")
    parser.add_argument("--port", help="serial port of the base station, e.g. COM5 or /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--code", help="pairing code from the app (otherwise you're asked for it)")
    parser.add_argument("--raw", action="store_true", help="print every raw line from the base station")
    args = parser.parse_args()

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
    print(f"Reading the wearable on {port}. Server: {args.server}")
    print("Ctrl+C stops.")
    state: dict = {}

    async def go() -> None:
        link = state["link"] = SerialLink(ser, asyncio.get_running_loop())
        bridge = Bridge(args, link, serial, list_ports, port)
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

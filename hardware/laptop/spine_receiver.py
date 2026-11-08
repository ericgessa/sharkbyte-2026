#!/usr/bin/env python3
"""
Spine receiver: reads JSON lines from the base station, shows them live,
and saves every reading to a CSV file.

Setup (once):   pip install pyserial
Run:            python spine_receiver.py              (finds the base station)
                python spine_receiver.py --port COM5  (or pick the port yourself)

While running:  type  c  then Enter  -> wearable recalibrates
                Ctrl+C               -> stop and save
"""

import argparse
import csv
import json
import sys
import threading
from datetime import datetime

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is missing. Install it with:  pip install pyserial")

PROMPTS = {
    "neutral":   "CALIBRATION 1/2: stand tall in neutral posture and hold STILL...",
    "bend_wait": "CALIBRATION 2/2: slowly bend FORWARD as far as is comfortable and hold...",
    "bend_hold": "  hold it...",
    "live":      "Calibrated. Live data in degrees (+ = bending forward):",
}

FIELDS = ["pc_time", "seq", "wearable_ms", "phase",
          "torso", "upper", "mid", "lumbar",
          "C", "B", "A", "BNO", "lost"]

# Words that usually appear in an ESP32's USB port description
PORT_HINTS = ("cp210", "ch340", "ch910", "wch", "silicon labs",
              "usb serial", "usb jtag", "espressif", "esp32")


def find_port():
    ports = list(list_ports.comports())
    for p in ports:
        desc = f"{p.description} {p.manufacturer or ''}".lower()
        if any(hint in desc for hint in PORT_HINTS):
            return p.device
    if len(ports) == 1:
        return ports[0].device
    print("Couldn't pick the base station's port automatically. Ports found:")
    for p in ports:
        print(f"  {p.device}: {p.description}")
    sys.exit("Run again with --port, for example:  python spine_receiver.py --port COM5")


def fmt(value):
    return "   -- " if value is None else f"{value:6.1f}"


def keyboard_commands(ser):
    """Runs in the background: forwards 'c' to the base station."""
    for line in sys.stdin:
        if line.strip().lower() == "c":
            ser.write(b"c")
            print("\n-> recalibration requested")


def main():
    parser = argparse.ArgumentParser(description="Receive spine data from the base station")
    parser.add_argument("--port", help="serial port of the base station, e.g. COM5 or /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--raw", action="store_true", help="also print every raw JSON line")
    args = parser.parse_args()

    port = args.port or find_port()
    try:
        ser = serial.Serial(port, args.baud, timeout=1)
    except serial.SerialException as e:
        sys.exit(f"Couldn't open {port}: {e}\n"
                 "Close the Arduino Serial Monitor if it's open on this port, then try again.")

    log_name = datetime.now().strftime("spine_%Y%m%d_%H%M%S.csv")
    log_file = open(log_name, "w", newline="")
    writer = csv.DictWriter(log_file, fieldnames=FIELDS)
    writer.writeheader()

    print(f"Listening on {port}. Logging to {log_name}")
    print("Type c + Enter to recalibrate, Ctrl+C to stop.\n")
    threading.Thread(target=keyboard_commands, args=(ser,), daemon=True).start()

    last_phase = None
    rows = 0
    try:
        while True:
            line = ser.readline().decode(errors="ignore").strip()
            if not line:
                continue
            if args.raw:
                print(line)

            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                print(f"\n[base] {line}")        # boot messages etc.
                continue

            if msg.get("type") == "status":
                print(f"\n[base] {msg.get('msg')}")
                continue
            if msg.get("type") != "data":
                continue

            phase = msg.get("phase")
            if phase != last_phase:
                print("\n" + PROMPTS.get(phase, f"phase: {phase}"))
                last_phase = phase

            sensors = msg.get("sensors", {})
            writer.writerow({
                "pc_time": datetime.now().isoformat(timespec="milliseconds"),
                "seq": msg.get("seq"),
                "wearable_ms": msg.get("t"),
                "phase": phase,
                "torso": msg.get("torso"),
                "upper": msg.get("upper"),
                "mid": msg.get("mid"),
                "lumbar": msg.get("lumbar"),
                "C": sensors.get("C"),
                "B": sensors.get("B"),
                "A": sensors.get("A"),
                "BNO": sensors.get("BNO"),
                "lost": msg.get("lost"),
            })
            rows += 1
            if rows % 50 == 0:                   # save to disk about once a second
                log_file.flush()

            if phase == "live" and not args.raw:
                print(f"\rtorso {fmt(msg.get('torso'))} | upper {fmt(msg.get('upper'))} | "
                      f"mid {fmt(msg.get('mid'))} | lumbar {fmt(msg.get('lumbar'))} | "
                      f"lost {msg.get('lost', 0)}   ", end="", flush=True)

    except KeyboardInterrupt:
        pass
    finally:
        log_file.close()
        ser.close()
        print(f"\n\nStopped. Saved {rows} readings to {log_name}")


if __name__ == "__main__":
    main()

# Hardware: spine sensing wearable

Compression-shirt wearable that measures spine bending during lifts.
Four IMUs run down the spine; the difference in tilt between neighbouring
sensors gives how much each section of the back is bending.

## Parts

| Part | Qty | Position |
|---|---|---|
| Seeed XIAO ESP32S3 | 1 | Hub, side of waistband |
| Adafruit BNO085 | 1 | Upper back (between shoulder blades) |
| MPU-6050 (GY-521) | 3 | Mid back (A), lower back (B), sacrum (C) |
| 3.7V LiPo + slide switch | 1 | Hub |
| Any ESP32 (spare XIAO or DevKit) | 1 | Base station, plugged into the laptop (no wiring) |

## Wiring (XIAO ESP32S3)

All sensors share one I2C bus. The three MPU-6050s share the same address,
so each one's AD0 pin gets its own select wire (the "AD0 trick"): the MPU
being read is pulled LOW (address 0x68), the others are held HIGH (0x69).

| Board pin | XIAO pin | ESP32 DevKit (backup) |
|---|---|---|
| All boards VIN/VCC | 3V3 | 3V3 |
| All boards GND | GND | GND |
| All boards SDA | D4 | GPIO21 |
| All boards SCL | D5 | GPIO22 |
| MPU A (mid back) AD0 | D0 | GPIO25 |
| MPU B (lower back) AD0 | D1 | GPIO26 |
| MPU C (sacrum) AD0 | D3 | GPIO27 |

I2C addresses: BNO085 0x4A, selected MPU 0x68, idle MPUs 0x69.

Leave unconnected: BNO085 PS0/PS1/INT/RST, GY-521 XDA/XCL/INT.

## Sketches

| Folder | Purpose |
|---|---|
| `firmware/spine_wearable` | **Main wearable sketch**: `spine_test` plus wireless streaming to the base station |
| `firmware/base_station` | Flash onto a second ESP32 plugged into the laptop; prints JSON over USB |
| `firmware/spine_test` | USB-only version of the wearable (no radio): all four sensors, calibration, section angles |
| `laptop/spine_receiver.py` | Reads the base station, shows live data, saves a CSV, sends recalibrate |
| `tests/bno_test` | BNO085 alone: I2C scan + yaw/pitch/roll |
| `tests/mpu_dual_test` | Two MPUs with AD0 select |
| `tests/mpu_select_check` | Proves two MPUs are really separate chips (tag test) |
| `tests/ad0_phase_check` | Holds each AD0 state for multimeter checks |

Library needed: **Adafruit BNO08x** (Arduino Library Manager). Board: **XIAO_ESP32S3**.
Serial Monitor at 115200 baud.

## Using `spine_test`

1. Calibrate (runs at startup, or send `c`): stand in neutral posture and hold still
   for 3 s, then slowly bend forward as far as is comfortable and hold.
2. Live output, in degrees (+ = bending forward vs neutral):
   - `torso`: whole upper-body lean (BNO085)
   - `upper`: BNO085 minus MPU A
   - `mid`: MPU A minus MPU B
   - `lumbar`: MPU B minus MPU C (the most important one for form)
3. Send `d` for the per-sensor debug view (each sensor's own tilt, plus BNO data rate).

A flat-back hip hinge raises `torso` while the sections stay near 0.
Rounding the lower back raises `lumbar`.

## Running wirelessly

```
wearable (spine_wearable) --ESP-NOW, 50 Hz--> base station (base_station) --USB, JSON lines--> laptop/spine_receiver.py
```

1. Flash `firmware/spine_wearable` onto the wearable and `firmware/base_station` onto the base ESP32.
   Both must share the same `PAIR_KEY` and `WIFI_CHANNEL`.
2. Close the Arduino Serial Monitor (only one program can hold the port), then:
   `pip install -r laptop/requirements.txt` and `python laptop/spine_receiver.py` (add `--port COM5` if needed).
3. Power the wearable; it pairs automatically and calibrates as above. Type `c` + Enter to recalibrate.
   Ctrl+C stops and saves `spine_<date>_<time>.csv` in the current folder.

Pairing: the base beacons twice a second until a wearable pairs, then sends a heartbeat twice a second.
If either side hears nothing for 3 s it drops the link and re-pairs on its own.

The base prints one JSON object per line (115200 baud):

```json
{"type":"data","seq":12,"t":4567,"phase":"live","torso":12.3,"upper":1.2,"mid":-0.4,"lumbar":5.6,"sensors":{"C":1.0,"B":6.6,"A":6.2,"BNO":7.4},"ok":15,"lost":0}
{"type":"status","msg":"paired with wearable AA:BB:CC:DD:EE:FF"}
```

- `null` = not available (still calibrating, or that sensor isn't working).
- `phase`: `neutral`, `bend_wait`, `bend_hold`, `live`.
- `ok`: bitmask of working sensors (1 = C, 2 = B, 4 = A, 8 = BNO; 15 = all). `lost`: packets missed since pairing.

## Troubleshooting

- BNO085 data rate low or 0 Hz: change `Wire.setClock(400000)` to `100000`.
- A section reacts to the wrong sensor: the AD0 wires don't match `MPU_AD0`
  (listed bottom to top: sacrum, lower back, mid back). Fix the wires or the array.
- Two MPUs reading identical values: both are answering at 0x68; run `tests/ad0_phase_check`.
- Receiver can't open the port: close the Arduino Serial Monitor.
- Base stays at "waiting for wearable": check the wearable is powered and both sketches share
  `PAIR_KEY` / `WIFI_CHANNEL`. The wearable's USB Serial Monitor shows `[radio] ...` status lines.
- `lost` climbing fast: move the base station closer or off the back of the laptop.

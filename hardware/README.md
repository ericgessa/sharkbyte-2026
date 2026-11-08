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
| Adafruit DRV2605L + coin motor | 1 | Hub / lower back (haptic feedback) |
| Any ESP32 (spare XIAO or DevKit) | 1 | Base station, plugged into the laptop (no wiring), for the wireless link |
| 3.7V LiPo + slide switch | 1 | Hub |

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

I2C addresses: BNO085 0x4A, DRV2605L 0x5A, selected MPU 0x68, idle MPUs 0x69.

Leave unconnected: BNO085 PS0/PS1/INT/RST, DRV2605L IN, GY-521 XDA/XCL/INT.

## Sketches

| Folder | Purpose |
|---|---|
| `firmware/spine_test` | USB-only sketch: all four sensors, calibration, spine section angles, `s` stream mode for the web page |
| `firmware/spine_wearable` | Wearable sketch with wireless streaming (ESP-NOW) to the base station |
| `firmware/base_station` | Flash onto a second ESP32 plugged into the laptop; prints JSON over USB |
| `firmware/spine_test` | USB-only version of the wearable (no radio): all four sensors, calibration, section angles |
| `laptop/spine_bridge.py` | Reads the base station and streams it to the backend (paired with a workout by a code from the app) |
| `laptop/spine_receiver.py` | Reads the base station, shows live data, saves a CSV, and (with `--upload`) sends it to the server |
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
3. Send `d` for the per-sensor debug view (each sensor's own tilt, plus BNO data rate), `n` to go back.
4. Send `s` for stream mode: one JSON line per sample (20/s), the format the app reads
   (see "Data format" below). The `/pose` page sends `s` for you when it connects.

`ROLL_SIGN` (in `spine_test` and `spine_wearable`): the app expects roll + = leaning to the wearer's right.
Lean right in the debug view; if "BNO side" goes negative, set `ROLL_SIGN` to `-1`. Not checked on a body yet.

A flat-back hip hinge raises `torso` while the sections stay near 0.
Rounding the lower back raises `lumbar`.

## Sending data to the server

The wearable connects over **USB**. The browser page that runs the workout reads it and forwards each
sample over the workout's WebSocket, which is the only connection the server allows per workout.

1. Flash `firmware/spine_test` and plug the XIAO into the laptop running the web app.
2. **Close the Arduino Serial Monitor.** Only one program can hold the port.
3. Open the app's `/pose` page in desktop **Chrome or Edge**, click **Connect wearable** and pick the
   XIAO's port. The page switches the firmware to stream mode (`s`) and shows calibration prompts.
4. Calibrate: stand still, then bend forward and hold, as above. **Recalibrate** on the page sends `c`.
5. Start a workout on the same page. While it runs, samples go to the server at ~20 per second.

In stream mode each line looks like this (a field is left out if its sensor is missing):

```
{"t":123456,"imu_pitch_deg":30.10,"imu_roll_deg":-2.00,"spine_upper_deg":4.20,"spine_mid_deg":-1.50,"spine_lumbar_deg":18.00}
```

The page wraps it in a `sensor_update`
([`docs/event-contracts.md`](../docs/event-contracts.md), [`server/app/schemas/sensors.py`](../server/app/schemas/sensors.py)):

| `spine_test` value | `sensor_update` field | Notes |
|---|---|---|
| `torso` (BNO085 forward tilt) | `imu_pitch_deg` | + = leaning forward, relative to the calibrated neutral posture |
| BNO085 side tilt | `imu_roll_deg` | The server expects + = toward the wearer's right. **Not checked yet:** lean right in the debug view (`d`); if `BNO side` goes negative, set `ROLL_SIGN` to `-1` in the sketch |
| `upper` | `spine_upper_deg` | BNO085 minus MPU A |
| `mid` | `spine_mid_deg` | MPU A minus MPU B |
| `lumbar` | `spine_lumbar_deg` | MPU B minus MPU C |
| `t` | `timestamp_ms` | ESP32 uptime in ms; the server anchors it to its own clock |

The project has no flex sensors, and yaw isn't sent because it drifts.

The server stores all five values in Tiger Data and uses the section angles for back-shape coaching
([`docs/form-iq.md`](../docs/form-iq.md)): `lumbar` drives the squat and push-up cues.

## Running wirelessly

```
wearable --ESP-NOW, 50 Hz--> base station --USB--> laptop/spine_bridge.py --WebSocket--> backend --> app (any device, phone too)
                                                                                             \--> Tiger Data
```

1. Flash `firmware/spine_wearable` onto the wearable and `firmware/base_station` onto the base ESP32.
   Both must share the same `PAIR_KEY` and `WIFI_CHANNEL`.
2. Close the Arduino Serial Monitor (only one program can hold the port).
3. **Into the app and database (recommended, works with the app on a phone):** plug the base station into the
   laptop, `pip install -r laptop/requirements.txt`, and run
   `python laptop/spine_bridge.py --server http://<backend address>:8000` (the same address the app uses; add
   `--port COM5` if the port isn't found). In the app's `/pose` page start a workout, press *Pair laptop bridge*
   and type the code it shows into the bridge. Readings are stored in Tiger Data and shown live in the app;
   *Recalibrate* in the app reaches the wearable. When the workout finishes the bridge asks for the next code.
4. **Or, with the app open in desktop Chrome/Edge on the same laptop:** press *Connect wearable (USB here)* and
   pick the port (Web Serial). The browser relays the readings itself.
5. **Or just log to a CSV on the laptop:** `pip install -r laptop/requirements.txt` and
   `python laptop/spine_receiver.py` (add `--port COM5` if needed). Type `c` + Enter to recalibrate;
   Ctrl+C stops and saves `spine_<date>_<time>.csv` in the current folder.
6. Power the wearable; it pairs automatically and calibrates as above.

Pairing: the base beacons twice a second until a wearable pairs, then sends a heartbeat twice a second.
If either side hears nothing for 3 s it drops the link and re-pairs on its own.

## Data format

`spine_test` (stream mode) and the base station print the same JSON lines, 115200 baud. The app's parser is
`mobile/src/pose/wearable.ts`; the server contract is `sensor_update` in `docs/event-contracts.md`.

```json
{"t":4567,"imu_pitch_deg":12.3,"imu_roll_deg":-2.0,"spine_upper_deg":1.2,"spine_mid_deg":-0.4,"spine_lumbar_deg":5.6,"seq":12,"phase":"live","ok":15,"lost":0}
{"type":"status","msg":"paired with wearable AA:BB:CC:DD:EE:FF"}
```

| Field | Meaning (degrees vs the calibrated neutral posture) |
|---|---|
| `t` | the wearable's own clock, ms since boot (the server re-anchors it) |
| `imu_pitch_deg` | whole upper-body lean (BNO085), + = forward. Was `torso` |
| `imu_roll_deg` | upper-back sideways tilt, + = toward the wearer's right (see `ROLL_SIGN`) |
| `spine_upper_deg` | BNO085 minus MPU A, + = bending forward. Was `upper` |
| `spine_mid_deg` | MPU A minus MPU B. Was `mid` |
| `spine_lumbar_deg` | MPU B minus MPU C (sacrum). Was `lumbar` |

- A field is **left out** when it isn't available (still calibrating, or that sensor isn't working).
- Base station only, ignored by the app: `seq`; `phase` (`neutral`, `bend_wait`, `bend_hold`, `live`);
  `ok` = bitmask of working sensors (1 = C, 2 = B, 4 = A, 8 = BNO; 15 = all); `lost` = packets missed since pairing.

### Sending the wireless data to the server (Tiger Data)

Two ways; both end in the same place (`sensor_update` on the workout's WebSocket, stored in Tiger Data):

1. **Browser (with the camera, so you also get back-shape coaching):** open `/pose`, click **Connect wearable** and
   pick the **base station's** port. The page understands the base station's lines (`torso`/`upper`/`mid`/`lumbar`,
   only once its `phase` is `live`) as well as `spine_test`'s stream, and forwards them over the one workout socket
   together with the camera's angles. The server judges each counted rep's back shape and sends the cue.
2. **No browser, sensor data only:** `pip install -r laptop/requirements.txt`, then
   `python laptop/spine_receiver.py --upload` (add `--api http://host:8000`, `--exercise squat`). It asks for your GRIT
   email and password (or set `GRIT_EMAIL` / `GRIT_PASSWORD`, or a ready `GRIT_TOKEN`; nothing is stored or printed),
   starts a workout, and forwards each live reading at up to 25 per second. Readings are stored in Tiger Data and the
   CSV is still written. Without hardware: `--replay lines.jsonl` feeds saved base-station lines instead of a port.
   If the server rejects the readings (for example it is running an old version) the receiver says so.

Back-shape cues need the camera to count the reps, so for coaching use way 1; way 2 is for collecting data.

## Troubleshooting

- BNO085 data rate low or 0 Hz: change `Wire.setClock(400000)` to `100000`.
- A section reacts to the wrong sensor: the AD0 wires don't match `MPU_AD0`
  (listed bottom to top: sacrum, lower back, mid back). Fix the wires or the array.
- Two MPUs reading identical values: both are answering at 0x68; run `tests/ad0_phase_check`.
- Bridge, receiver or `/pose` page can't open the port: close the Arduino Serial Monitor and the other two.
- Base stays at "waiting for wearable": check the wearable is powered and both sketches share
  `PAIR_KEY` / `WIFI_CHANNEL`. The wearable's USB Serial Monitor shows `[radio] ...` status lines.
- `lost` climbing fast: move the base station closer or off the back of the laptop.

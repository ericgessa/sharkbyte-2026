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
| `firmware/spine_test` | Main sketch: all four sensors, calibration, spine section angles |
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
3. Serial commands: `c` recalibrate, `n` normal view, `d` per-sensor debug view (each sensor's own
   tilt, plus BNO data rate), `s` stream mode (one JSON line per sample, for the server).

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

The server stores all five values in Tiger Data. The posture coach ([`docs/ai-coach.md`](../docs/ai-coach.md))
still only looks at `imu_pitch_deg` / `imu_roll_deg` and isn't wired into the live workout yet. Using the
section angles, especially `lumbar`, for coaching is the next step.

Not built yet:

- **Wireless.** USB only for now; BLE could reuse the same JSON lines.
- **Haptics.** The DRV2605L is in the parts list but no sketch drives it.

Answers to the open hardware questions in `docs/ai-coach.md`: the BNO085 sits between the shoulder
blades; axis direction doesn't matter, because calibration learns each sensor's forward direction;
there's no flex sensor; sensors are sampled at 100 Hz on the board and sent at 20 Hz.

## Troubleshooting

- BNO085 data rate low or 0 Hz: change `Wire.setClock(400000)` to `100000`.
- A section reacts to the wrong sensor: the AD0 wires don't match `MPU_AD0`
  (listed bottom to top: sacrum, lower back, mid back). Fix the wires or the array.
- Two MPUs reading identical values: both are answering at 0x68; run `tests/ad0_phase_check`.

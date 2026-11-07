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
3. Send `d` for the per-sensor debug view (each sensor's own tilt, plus BNO data rate).

A flat-back hip hinge raises `torso` while the sections stay near 0.
Rounding the lower back raises `lumbar`.

## How this connects to the server

The backend already accepts wearable data as `sensor_update` messages on the workout WebSocket
([`docs/event-contracts.md`](../docs/event-contracts.md), [`server/app/schemas/sensors.py`](../server/app/schemas/sensors.py)).
The posture coach ([`docs/ai-coach.md`](../docs/ai-coach.md)) was built on simulated data and assumes
**one IMU flat on the upper back, between the shoulder blades**. That is the BNO085's position here.

| `spine_test` value | `sensor_update` field | Notes |
|---|---|---|
| BNO085 forward angle (`torso`) | `imu_pitch_deg` | Both use + = leaning forward. The server's field is described as tilt from vertical, while `torso` is measured from the calibrated neutral posture. |
| BNO085 side angle (debug view `BNO side`) | `imu_roll_deg` | The server expects + = toward the user's right. The sketch's sign hasn't been checked yet; flip it if it reads the other way. |
| `upper` / `mid` / `lumbar` | none yet | The contract has no per-section fields. Adding them means changing `SensorData` and `SENSOR_FIELDS` in `sensors.py`. |
| none | `flex_raw` | No flex sensor in the parts list. |
| none | `imu_yaw_deg` | Not used; yaw drifts without a magnetometer reference. |

Not built yet:

- **Transport.** The sketch only prints to Serial. Nothing sends `sensor_update` messages to the
  server yet (no Wi-Fi or BLE code).
- **Haptics.** The DRV2605L is in the parts list but no sketch drives it.

Answers to the open hardware questions in `docs/ai-coach.md`: the BNO085 sits between the shoulder
blades; axis direction doesn't matter, because calibration learns each sensor's forward direction;
there's no flex sensor; sensors are sampled at 100 Hz on the board (send rate not decided).

## Troubleshooting

- BNO085 data rate low or 0 Hz: change `Wire.setClock(400000)` to `100000`.
- A section reacts to the wrong sensor: the AD0 wires don't match `MPU_AD0`
  (listed bottom to top: sacrum, lower back, mid back). Fix the wires or the array.
- Two MPUs reading identical values: both are answering at 0x68; run `tests/ad0_phase_check`.

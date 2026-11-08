from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.events import PoseData
from app.schemas.sensors import SensorData, SensorReading, SensorUpdate
from app.services.ingest import ClockAnchors, pose_readings, sensor_readings

SID = uuid4()
T = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)


def update(**over):
    base = {
        "schema_version": 1,
        "type": "sensor_update",
        "session_id": str(SID),
        "device_id": "esp32-01",
        "sequence": 1,
        "timestamp_ms": 5000,
        "data": {"imu_pitch_deg": 12.4, "spine_lumbar_deg": 3.5},
    }
    base.update(over)
    return base


# ---- validation -------------------------------------------------------------


def test_valid_sensor_update_parses():
    u = SensorUpdate.model_validate(update())
    assert u.device_id == "esp32-01" and u.data.imu_pitch_deg == 12.4 and u.data.spine_lumbar_deg == 3.5


def test_full_spine_wearable_sample_parses():
    data = {"imu_pitch_deg": 30.1, "imu_roll_deg": -2.0, "spine_upper_deg": 4.2, "spine_mid_deg": -1.5, "spine_lumbar_deg": 18.0}
    u = SensorUpdate.model_validate(update(data=data))
    assert u.data.model_dump(exclude_none=True) == data


@pytest.mark.parametrize(
    "over",
    [
        {"data": {}},  # no values at all
        {"data": {"imu_pitch_deg": 9999}},  # impossible angle
        {"data": {"spine_lumbar_deg": 181}},  # a section can't bend more than half a turn
        {"data": {"spine_upper_deg": -181}},
        {"data": {"flex_raw": 1780}},  # flex sensors are not part of this project
        {"data": {"accel_x_g": 500}},  # beyond any real accelerometer range
        {"device_id": ""},
        {"device_id": "bad id with spaces"},
        {"device_id": "x" * 65},
        {"sequence": -1},
        {"data": {"spine_mid_deg": 1, "surprise": 1}},  # unknown field
        {"unexpected": True},
    ],
)
def test_invalid_sensor_updates_are_rejected(over):
    with pytest.raises(ValidationError):
        SensorUpdate.model_validate(update(**over))


def test_nan_and_infinity_are_rejected():
    raw = (
        '{"schema_version":1,"type":"sensor_update","session_id":"%s","device_id":"d","sequence":1,'
        '"timestamp_ms":1,"data":{"spine_lumbar_deg":NaN}}' % SID
    )
    with pytest.raises(ValidationError):
        SensorUpdate.model_validate_json(raw)
    with pytest.raises(ValidationError):
        SensorReading(time=T, session_id=SID, source="imu", metric_name="imu_pitch", metric_value=float("inf"))


def test_reading_requires_tz_aware_time_known_source_and_clean_metric_name():
    ok = dict(session_id=SID, source="camera", metric_name="left_knee_angle", metric_value=90.0)
    SensorReading(time=T, **ok)
    with pytest.raises(ValidationError):
        SensorReading(time=T.replace(tzinfo=None), **ok)
    with pytest.raises(ValidationError):
        SensorReading(time=T, **{**ok, "source": "thermometer"})
    with pytest.raises(ValidationError):
        SensorReading(time=T, **{**ok, "source": "flex_sensor"})  # removed: no flex sensors in this project
    for bad in ("Left Knee", "x; drop table", "", "a" * 65):
        with pytest.raises(ValidationError):
            SensorReading(time=T, **{**ok, "metric_name": bad})


# ---- message -> readings (session association) -----------------------------


def test_pose_readings_carry_session_id_source_and_confidence():
    rs = pose_readings(SID, T, PoseData(left_knee_angle_deg=91.5, right_knee_angle_deg=93.2, landmark_confidence=0.9))
    assert {r.metric_name: r.metric_value for r in rs} == {"left_knee_angle": 91.5, "right_knee_angle": 93.2}
    assert all(r.session_id == SID and r.source == "camera" and r.time == T for r in rs)
    assert all(r.metadata == {"confidence": 0.9} for r in rs)


def test_pose_readings_skip_a_knee_that_was_not_visible():
    rs = pose_readings(SID, T, PoseData(left_knee_angle_deg=None, right_knee_angle_deg=88.0, landmark_confidence=0.8))
    assert [r.metric_name for r in rs] == ["right_knee_angle"]


def test_sensor_readings_map_fields_sources_and_device_id():
    data = SensorData(spine_lumbar_deg=18.0, spine_upper_deg=4.2, imu_pitch_deg=12.4, accel_z_g=0.98)
    rs = {r.metric_name: r for r in sensor_readings(SID, T, "esp32-01", data)}
    assert set(rs) == {"spine_lumbar", "spine_upper", "imu_pitch", "accel_z"}
    assert rs["spine_lumbar"].metric_value == 18.0
    assert all(r.source == "imu" for r in rs.values())
    assert all(r.sensor_id == "esp32-01" and r.session_id == SID for r in rs.values())


# ---- clock anchoring --------------------------------------------------------


def test_clock_anchor_preserves_spacing_and_lands_on_server_time():
    now = [1_800_000_000.0]
    clock = ClockAnchors(now=lambda: now[0])
    t0 = clock.to_datetime("camera", 5_000)  # device clock starts at an arbitrary 5 s
    now[0] += 0.5
    t1 = clock.to_datetime("camera", 5_500)
    assert t0.timestamp() == pytest.approx(1_800_000_000.0)
    assert (t1 - t0).total_seconds() == pytest.approx(0.5)
    assert t0.tzinfo is not None


def test_each_source_has_its_own_offset():
    clock = ClockAnchors(now=lambda: 1_800_000_000.0)
    cam = clock.to_datetime("camera", 1_000)
    dev = clock.to_datetime("device:esp32-01", 9_999_000)  # wildly different clock
    assert cam == dev  # both anchored to the same server instant


def test_a_device_clock_jump_re_anchors_instead_of_writing_far_future_data():
    now = [1_800_000_000.0]
    clock = ClockAnchors(now=lambda: now[0])
    clock.to_datetime("device:d", 1_000_000)  # the ESP32 has been up for ~17 minutes
    now[0] += 1
    reboot = clock.to_datetime("device:d", 50)  # then rebooted: its clock restarted from ~0
    assert abs(reboot.timestamp() - now[0]) < 1  # stamped "now", not 17 minutes in the past

"""Time-series models: the ESP32 ``sensor_update`` message and the ``SensorReading`` row stored in Tiger Data."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SourceName = Literal["camera", "imu"]

METRIC_NAME_PATTERN = r"^[a-z0-9_]{1,64}$"

# Field name in a sensor_update  ->  (source, stored metric_name)
SENSOR_FIELDS: dict[str, tuple[str, str]] = {
    "spine_upper_deg": ("imu", "spine_upper"),
    "spine_mid_deg": ("imu", "spine_mid"),
    "spine_lumbar_deg": ("imu", "spine_lumbar"),
    "imu_pitch_deg": ("imu", "imu_pitch"),
    "imu_roll_deg": ("imu", "imu_roll"),
    "imu_yaw_deg": ("imu", "imu_yaw"),
    "accel_x_g": ("imu", "accel_x"),
    "accel_y_g": ("imu", "accel_y"),
    "accel_z_g": ("imu", "accel_z"),
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SensorData(_Strict):
    """One ESP32 sample from the spine wearable (hardware/README.md). Units: degrees for angles, g for acceleration.

    ``spine_*_deg`` is the bend WITHIN one section of the back (the tilt of the sensor above it minus the sensor
    below it), + = bending forward, relative to the wearer's calibrated neutral posture:
    upper = BNO085 minus MPU A, mid = MPU A minus MPU B, lumbar = MPU B minus MPU C (sacrum).
    ``imu_pitch_deg`` / ``imu_roll_deg`` are the upper-back (BNO085) forward and sideways tilt.

    Readings are measured tilt, not validated anatomical angles; they are stored as measured.
    """

    spine_upper_deg: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    spine_mid_deg: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    spine_lumbar_deg: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    imu_pitch_deg: float | None = Field(default=None, ge=-360, le=360, allow_inf_nan=False)
    imu_roll_deg: float | None = Field(default=None, ge=-360, le=360, allow_inf_nan=False)
    imu_yaw_deg: float | None = Field(default=None, ge=-360, le=360, allow_inf_nan=False)
    accel_x_g: float | None = Field(default=None, ge=-64, le=64, allow_inf_nan=False)
    accel_y_g: float | None = Field(default=None, ge=-64, le=64, allow_inf_nan=False)
    accel_z_g: float | None = Field(default=None, ge=-64, le=64, allow_inf_nan=False)

    @model_validator(mode="after")
    def _at_least_one_value(self) -> "SensorData":
        if all(getattr(self, name) is None for name in SENSOR_FIELDS):
            raise ValueError("at least one sensor value is required")
        return self


class SensorUpdate(_Strict):
    """Sent on the existing authenticated workout WebSocket (e.g. a phone relaying ESP32 data)."""

    schema_version: Literal[1]
    type: Literal["sensor_update"]
    session_id: UUID
    device_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    sequence: int = Field(ge=0)
    timestamp_ms: int = Field(ge=0)  # the DEVICE's clock; anchored to server time on arrival
    data: SensorData


class SensorReading(_Strict):
    """One stored measurement (a row of the sensor_readings hypertable)."""

    time: datetime
    session_id: UUID
    source: SourceName
    sensor_id: str = Field(default="", max_length=64)
    metric_name: str = Field(pattern=METRIC_NAME_PATTERN)
    metric_value: float = Field(allow_inf_nan=False)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("time")
    @classmethod
    def _timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("time must be timezone-aware")
        return v

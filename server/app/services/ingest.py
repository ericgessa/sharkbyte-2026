"""Sensor ingestion: map device clocks to server time, buffer readings, write them to Tiger Data in batches.

Design rules (AGENTS.md: the fast motion loop must never wait on slow storage):
  * ``add()`` is synchronous and never blocks or raises: it appends to a bounded in-memory buffer.
  * A background task flushes batches to the sink off the event loop (``asyncio.to_thread``).
  * If the sink fails, batches stay in the buffer and are retried with exponential backoff. When the
    buffer is full the OLDEST readings are dropped (and counted): live feedback always wins over history.
  * Nothing here talks to Supabase, and a Tiger Data outage never affects rep counting or coaching.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from app.schemas.events import PoseData
from app.schemas.sensors import SENSOR_FIELDS, SensorData, SensorReading

logger = logging.getLogger(__name__)

# If a source's mapped time is further than this from "now" (device rebooted, clock jumped),
# re-anchor instead of writing readings far in the past or future.
RESYNC_THRESHOLD_MS = 60_000


# ------------------------------------------------------------------ clock anchoring


class ClockAnchors:
    """Maps each source's own clock onto server time.

    Camera frames and each ESP32 have unrelated clocks, so the first message of a source fixes an
    offset (server_now - source_ts). After that the SPACING between that source's samples is exact;
    alignment BETWEEN sources is only as good as network latency (tens of ms), so analytics should use
    time windows rather than assume simultaneous samples.
    """

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._offsets_ms: dict[str, float] = {}

    def to_datetime(self, source_key: str, client_ts_ms: int) -> datetime:
        now_ms = self._now() * 1000
        offset = self._offsets_ms.get(source_key)
        if offset is None or abs(client_ts_ms + offset - now_ms) > RESYNC_THRESHOLD_MS:
            offset = now_ms - client_ts_ms
            self._offsets_ms[source_key] = offset
        return datetime.fromtimestamp((client_ts_ms + offset) / 1000, tz=timezone.utc)


# ------------------------------------------------------------------ message -> readings


def pose_readings(session_id: UUID, when: datetime, data: PoseData) -> list[SensorReading]:
    """Camera angles. Landmark confidence rides in metadata so analytics can filter unreliable frames."""
    meta = {"confidence": data.landmark_confidence}
    out: list[SensorReading] = []
    for metric, value in (("left_knee_angle", data.left_knee_angle_deg), ("right_knee_angle", data.right_knee_angle_deg)):
        if value is not None:
            out.append(
                SensorReading(time=when, session_id=session_id, source="camera", metric_name=metric, metric_value=value, metadata=meta)
            )
    return out


def sensor_readings(session_id: UUID, when: datetime, device_id: str, data: SensorData) -> list[SensorReading]:
    out: list[SensorReading] = []
    for field_name, (source, metric) in SENSOR_FIELDS.items():
        value = getattr(data, field_name)
        if value is not None:
            out.append(
                SensorReading(
                    time=when, session_id=session_id, source=source, sensor_id=device_id, metric_name=metric, metric_value=value
                )
            )
    return out


# ------------------------------------------------------------------ buffer + flusher


class Sink(Protocol):
    def insert_readings(self, readings: Sequence[SensorReading]) -> int: ...


@dataclass
class IngestStats:
    accepted: int = 0  # readings handed to add()
    inserted: int = 0  # rows the database actually stored (duplicates excluded)
    dropped: int = 0  # discarded because the buffer was full during an outage
    failed_batches: int = 0
    buffered: int = 0
    healthy: bool = True


class SensorIngestor:
    def __init__(
        self,
        sink: Sink | None,
        *,
        batch_size: int = 500,
        flush_interval_s: float = 1.0,
        max_buffered: int = 20_000,
        max_backoff_s: float = 30.0,
    ) -> None:
        self._sink = sink
        self._batch_size = batch_size
        self._interval = flush_interval_s
        self._max_buffered = max_buffered
        self._max_backoff = max_backoff_s
        self._buffer: deque[SensorReading] = deque()
        self._stats = IngestStats()
        self._backoff_s = 0.0
        self._retry_at = 0.0
        self._wake: asyncio.Event | None = None
        self._lock: asyncio.Lock | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    # -- public API ------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._sink is not None

    @property
    def stats(self) -> IngestStats:
        self._stats.buffered = len(self._buffer)
        return self._stats

    def add(self, readings: Sequence[SensorReading]) -> None:
        """Queue readings. Never blocks, never raises. No-op when no sink is configured."""
        if self._sink is None or not readings:
            return
        self._stats.accepted += len(readings)
        self._buffer.extend(readings)
        self._trim()
        if len(self._buffer) >= self._batch_size and self._wake is not None:
            self._wake.set()

    async def start(self) -> None:
        if self._sink is None or self._task is not None:
            return
        self._wake, self._lock = asyncio.Event(), asyncio.Lock()
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="sensor-ingestor")

    async def stop(self, timeout_s: float = 5.0) -> None:
        """Stop the flusher after one last best-effort flush."""
        if self._task is None:
            return
        self._stopping = True
        if self._wake is not None:
            self._wake.set()
        try:
            await asyncio.wait_for(self._task, timeout_s + 1)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None
        await self.flush(timeout_s)

    async def flush(self, timeout_s: float = 5.0) -> bool:
        """Try to write everything buffered right now (used when a workout finishes).

        Ignores any backoff. Returns True if the buffer is empty afterwards. Never raises.
        """
        if self._sink is None:
            return True
        if self._lock is None:
            self._lock = asyncio.Lock()
        try:
            await asyncio.wait_for(self._drain(respect_backoff=False), timeout_s)
        except asyncio.TimeoutError:
            logger.warning("sensor flush timed out with %d readings still buffered", len(self._buffer))
        return not self._buffer

    # -- internals -------------------------------------------------------

    def _trim(self) -> None:
        overflow = len(self._buffer) - self._max_buffered
        if overflow > 0:
            for _ in range(overflow):
                self._buffer.popleft()  # keep the newest data
            if self._stats.dropped == 0:  # log the first drop only; the count is in stats
                logger.warning("sensor buffer full: dropping oldest readings (Tiger Data unreachable?)")
            self._stats.dropped += overflow

    async def _run(self) -> None:
        assert self._wake is not None
        while not self._stopping:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            await self._drain(respect_backoff=True)
        await self._drain(respect_backoff=False)

    async def _drain(self, *, respect_backoff: bool) -> None:
        assert self._lock is not None
        async with self._lock:
            while self._buffer:
                if respect_backoff and time.monotonic() < self._retry_at:
                    return
                if not await self._flush_one():
                    return

    async def _flush_one(self) -> bool:
        """Write one batch. On failure put it back at the FRONT of the buffer and back off."""
        n = min(self._batch_size, len(self._buffer))
        batch = [self._buffer.popleft() for _ in range(n)]
        try:
            inserted = await asyncio.to_thread(self._sink.insert_readings, batch)  # type: ignore[union-attr]
        except Exception as exc:
            self._buffer.extendleft(reversed(batch))
            self._trim()
            self._stats.failed_batches += 1
            self._backoff_s = min(self._max_backoff, max(1.0, self._backoff_s * 2))
            self._retry_at = time.monotonic() + self._backoff_s
            if self._stats.healthy:  # log once per outage, not once per attempt
                logger.warning("Tiger Data write failed (%s); buffering and retrying", type(exc).__name__)
            self._stats.healthy = False
            return False
        if not self._stats.healthy:
            logger.info("Tiger Data writes recovered")
        self._stats.healthy = True
        self._backoff_s = 0.0
        self._stats.inserted += inserted
        return True

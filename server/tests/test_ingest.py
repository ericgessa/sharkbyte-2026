import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from app.schemas.sensors import SensorReading
from app.services.ingest import SensorIngestor

SID = uuid4()


def readings(n, start=0):
    return [
        SensorReading(
            time=datetime.fromtimestamp(1_800_000_000 + (start + i) / 1000, tz=timezone.utc),
            session_id=SID,
            source="camera",
            metric_name="left_knee_angle",
            metric_value=float(start + i),
        )
        for i in range(n)
    ]


class Sink:
    def __init__(self):
        self.batches, self.fail, self.calls = [], False, 0

    def insert_readings(self, batch):
        self.calls += 1
        if self.fail:
            raise RuntimeError("down")
        self.batches.append(list(batch))
        return len(batch)

    @property
    def values(self):
        return [r.metric_value for b in self.batches for r in b]


def run(coro):
    return asyncio.run(coro)


def test_flush_writes_everything_in_bounded_batches_in_order():
    async def go():
        sink = Sink()
        ing = SensorIngestor(sink, batch_size=500, flush_interval_s=60)
        ing.add(readings(1200))
        assert await ing.flush()
        return sink, ing

    sink, ing = run(go())
    assert [len(b) for b in sink.batches] == [500, 500, 200]
    assert sink.values == [float(i) for i in range(1200)]  # nothing lost, nothing reordered
    assert ing.stats.inserted == 1200 and ing.stats.buffered == 0


def test_reaching_batch_size_flushes_without_waiting_for_the_interval():
    async def go():
        sink = Sink()
        ing = SensorIngestor(sink, batch_size=5, flush_interval_s=60)  # the interval would be a minute
        await ing.start()
        ing.add(readings(5))
        for _ in range(100):
            if sink.batches:
                break
            await asyncio.sleep(0.02)
        await ing.stop()
        return sink

    assert run(go()).values == [0.0, 1.0, 2.0, 3.0, 4.0]


def test_interval_flushes_a_partial_batch():
    async def go():
        sink = Sink()
        ing = SensorIngestor(sink, batch_size=500, flush_interval_s=0.05)
        await ing.start()
        ing.add(readings(3))
        for _ in range(100):
            if sink.batches:
                break
            await asyncio.sleep(0.02)
        await ing.stop()
        return sink

    assert run(go()).values == [0.0, 1.0, 2.0]


def test_outage_never_raises_keeps_data_and_delivers_after_recovery():
    async def go():
        sink = Sink()
        sink.fail = True
        ing = SensorIngestor(sink, batch_size=10, flush_interval_s=60)
        ing.add(readings(25))  # add() must not raise
        ok = await ing.flush(timeout_s=2)  # flush() must not raise either
        assert ok is False
        assert ing.stats.buffered == 25 and ing.stats.failed_batches >= 1 and ing.stats.healthy is False
        sink.fail = False
        assert await ing.flush(timeout_s=2)
        return sink, ing

    sink, ing = run(go())
    assert sink.values == [float(i) for i in range(25)]  # original order, no loss, no duplicates
    assert ing.stats.healthy is True and ing.stats.dropped == 0


def test_full_buffer_during_outage_drops_the_oldest_and_keeps_the_newest():
    async def go():
        sink = Sink()
        sink.fail = True
        ing = SensorIngestor(sink, batch_size=10, flush_interval_s=60, max_buffered=20)
        ing.add(readings(50))
        sink.fail = False
        await ing.flush()
        return sink, ing

    sink, ing = run(go())
    assert ing.stats.dropped == 30 and ing.stats.accepted == 50
    assert sink.values == [float(i) for i in range(30, 50)]  # the newest 20


def test_background_loop_backs_off_instead_of_hammering_a_dead_database():
    async def go():
        sink = Sink()
        sink.fail = True
        ing = SensorIngestor(sink, batch_size=1, flush_interval_s=0.01)
        await ing.start()
        ing.add(readings(3))
        await asyncio.sleep(0.6)
        calls = sink.calls
        sink.fail = False
        await ing.stop()
        return calls

    assert run(go()) <= 3  # ~1 s minimum backoff: a handful of attempts, not hundreds


def test_without_a_sink_everything_is_a_harmless_noop():
    async def go():
        ing = SensorIngestor(None)
        ing.add(readings(10))
        await ing.start()
        ok = await ing.flush()
        await ing.stop()
        return ing, ok

    ing, ok = run(go())
    assert ok and not ing.enabled and ing.stats.accepted == 0


def test_stop_flushes_what_is_left():
    async def go():
        sink = Sink()
        ing = SensorIngestor(sink, batch_size=500, flush_interval_s=60)
        await ing.start()
        ing.add(readings(7))
        await ing.stop()
        return sink

    assert run(go()).values == [float(i) for i in range(7)]

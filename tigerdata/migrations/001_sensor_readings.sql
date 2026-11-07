-- Tiger Data (TimescaleDB) schema, part 1: the sensor_readings hypertable.
-- Requires TimescaleDB 2.20+ (CREATE TABLE ... WITH (tsdb.hypertable)); Tiger Cloud services have it.
--
-- This is a SEPARATE database from Supabase. session_id is the Supabase workout_sessions.id
-- UUID, but there is no foreign key (a database cannot reference another one); the backend
-- only writes readings for sessions it has already verified in Supabase.

create table sensor_readings (
    time         timestamptz      not null,
    session_id   uuid             not null,
    source       text             not null check (source in ('camera', 'flex_sensor', 'imu')),
    sensor_id    text             not null default '',   -- hardware id, e.g. 'esp32-01'; '' if none
    metric_name  text             not null check (metric_name ~ '^[a-z0-9_]{1,64}$'),
    metric_value double precision not null,
    metadata     jsonb            not null default '{}'::jsonb check (jsonb_typeof(metadata) = 'object')
) with (
    tsdb.hypertable,
    tsdb.partition_column = 'time',
    tsdb.chunk_interval = '1 day',
    -- Columnstore (compression) layout: a session's metric is read together, ordered by time.
    -- Tiger Data adds a columnstore policy automatically (chunks convert after one chunk interval).
    tsdb.segmentby = 'session_id, metric_name',
    tsdb.orderby = 'time DESC'
);

-- A retried batch must not create duplicates: the same measurement (same session, metric,
-- sensor and instant) can exist only once. Unique indexes on a hypertable must include "time".
create unique index sensor_readings_dedup
    on sensor_readings (session_id, metric_name, sensor_id, time);

-- Per-session, per-metric reads in time order (the shape of every analytics query).
create index sensor_readings_session_metric_time
    on sensor_readings (session_id, metric_name, time desc);

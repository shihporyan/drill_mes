-- Drill Monitoring System - SQLite Schema
-- Version: 1.0

-- ===== Core Tables =====

-- Hourly utilization + hole count
CREATE TABLE IF NOT EXISTS hourly_utilization (
    machine_id    TEXT NOT NULL,     -- 'M01'~'M18', 'L1'~'L4'
    date          TEXT NOT NULL,     -- 'YYYY-MM-DD'
    hour          INTEGER NOT NULL,  -- 0~23
    run_seconds   INTEGER DEFAULT 0,
    reset_seconds INTEGER DEFAULT 0,
    stop_seconds  INTEGER DEFAULT 0,
    total_seconds INTEGER DEFAULT 0,
    utilization   REAL DEFAULT 0.0,  -- 0.0~100.0
    hole_count    INTEGER DEFAULT 0,
    PRIMARY KEY (machine_id, date, hour)
);

-- Machine current state (overwritten by parser each cycle)
CREATE TABLE IF NOT EXISTS machine_current_state (
    machine_id    TEXT PRIMARY KEY,
    state         TEXT,              -- 'RUN' / 'RESET' / 'STOP'
    mode          TEXT,              -- 'AUTO' / 'MAN'
    program       TEXT,              -- 'O100.txt' / ''
    tool_num      TEXT,              -- '084' / '000'
    drill_dia     REAL,              -- 0.150
    since         TEXT,              -- ISO timestamp: state start time
    last_update   TEXT,              -- ISO timestamp: last update
    counter       INTEGER,           -- col 10 cumulative value
    work_order    TEXT,              -- e.g. 'O2604016' (Takeuchi) or 'WD-2604008-TOP-A' (Kataoka)
    work_order_side TEXT             -- 'B' (bottom) or 'T' (top)
);

-- State transition events (for downtime analysis)
CREATE TABLE IF NOT EXISTS state_transitions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id    TEXT NOT NULL,
    timestamp     TEXT NOT NULL,     -- ISO timestamp
    from_state    TEXT,
    to_state      TEXT NOT NULL,
    program       TEXT,
    tool_num      TEXT,
    drill_dia     REAL
);

-- ===== System Tables =====

-- Connection health monitoring
CREATE TABLE IF NOT EXISTS machine_health (
    machine_id        TEXT PRIMARY KEY,
    is_online         INTEGER DEFAULT 0,  -- 0/1
    last_seen         TEXT,               -- Last successful communication
    offline_since     TEXT,               -- NULL = online
    consecutive_fails INTEGER DEFAULT 0,
    last_check        TEXT
);

-- Incremental parse progress (prevent duplicate parsing)
CREATE TABLE IF NOT EXISTS parse_progress (
    machine_id    TEXT NOT NULL,
    day_prefix    TEXT NOT NULL,     -- '01'~'31'
    last_line     INTEGER DEFAULT 0,
    last_timestamp TEXT,
    file_size     INTEGER DEFAULT 0,
    PRIMARY KEY (machine_id, day_prefix)
);

-- Laser work order tracking (from ProcTimeEnd / ProcTimeStart)
CREATE TABLE IF NOT EXISTS laser_work_orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id    TEXT NOT NULL,
    start_time    TEXT NOT NULL,      -- ISO timestamp
    end_time      TEXT,               -- ISO timestamp, NULL if in-progress
    duration_secs REAL,               -- processing seconds
    station       TEXT,               -- station number '2', '3', '5' etc.
    work_order    TEXT NOT NULL,      -- e.g. 'WD-2604008-TOP-A'
    lsr_file_path TEXT,               -- original path on control PC
    hole_count    INTEGER DEFAULT 0,  -- sum of Count values from LSR file
    UNIQUE(machine_id, start_time, station)
);

-- System key-value store (e.g. next_cycle_at for frontend sync)
CREATE TABLE IF NOT EXISTS system_status (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ===== Flush Latency Instrumentation =====
-- See notes/tx1_flush_latency_investigation.md

-- Per-event TX1 FILEOPERATION LOAD detection latency
CREATE TABLE IF NOT EXISTS tx1_event_latency (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id     TEXT NOT NULL,
    event_ts       TEXT NOT NULL,      -- timestamp inside log line
    detected_at    TEXT NOT NULL,      -- server time when parser first saw it
    delay_seconds  REAL NOT NULL,      -- detected_at - event_ts
    program_name   TEXT,               -- NAME:[...] content
    wo_matched     INTEGER NOT NULL,   -- 0/1 passes WO_PATTERN
    UNIQUE(machine_id, event_ts, program_name)
);

-- Per-cycle size/mtime snapshot of each remote log file type (6 types for Takeuchi)
CREATE TABLE IF NOT EXISTS log_file_observe (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id   TEXT NOT NULL,
    log_type     TEXT NOT NULL,   -- 'Drive','TX1','MACRO','TARN','FILE','Alarm'
    observed_at  TEXT NOT NULL,
    file_size    INTEGER,
    file_mtime   TEXT,
    error        TEXT             -- non-null when stat failed
);

-- High-frequency (30s) TX1.Log mtime CHANGE events. Only inserts when the
-- mtime observed via SMB actually advances, so the table stays small and
-- each row represents "a flush was visible on SMB at this time."
-- Used to validate the hypothesis that state transitions trigger TX1 flushes
-- (cross-join with state_transitions to measure transition→flush latency).
CREATE TABLE IF NOT EXISTS tx1_mtime_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id   TEXT NOT NULL,
    observed_at  TEXT NOT NULL,    -- server time when change first detected
    new_mtime    TEXT NOT NULL,    -- SMB-reported mtime after the change
    size_delta   INTEGER,          -- bytes added since previous mtime
    new_size     INTEGER,
    UNIQUE(machine_id, new_mtime)
);

-- ===== Indexes =====
CREATE INDEX IF NOT EXISTS idx_hourly_date ON hourly_utilization(date);
CREATE INDEX IF NOT EXISTS idx_transitions_ts ON state_transitions(machine_id, timestamp);
-- Unique index: prevents duplicate transitions when a Drive.Log batch re-parses
-- a timestamp window already seen in a previous batch (firmware peek-ahead replay).
-- Paired with INSERT OR IGNORE in drive_log_parser.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_transitions_machine_ts
    ON state_transitions(machine_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_laser_wo_machine ON laser_work_orders(machine_id, start_time);
CREATE INDEX IF NOT EXISTS idx_tx1_latency_machine ON tx1_event_latency(machine_id, detected_at);
CREATE INDEX IF NOT EXISTS idx_log_observe_machine ON log_file_observe(machine_id, log_type, observed_at);
CREATE INDEX IF NOT EXISTS idx_tx1_mtime_machine ON tx1_mtime_events(machine_id, observed_at);

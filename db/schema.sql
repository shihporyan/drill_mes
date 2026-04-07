-- Drill Monitoring System - SQLite Schema
-- Version: 1.0

-- ===== Core Tables =====

-- Hourly utilization + hole count
CREATE TABLE IF NOT EXISTS hourly_utilization (
    machine_id    TEXT NOT NULL,     -- 'M01'~'M18'
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
    counter       INTEGER            -- col 10 cumulative value
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

-- ===== Indexes =====
CREATE INDEX IF NOT EXISTS idx_hourly_date ON hourly_utilization(date);
CREATE INDEX IF NOT EXISTS idx_transitions_ts ON state_transitions(machine_id, timestamp);

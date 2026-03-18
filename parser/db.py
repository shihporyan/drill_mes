"""SQLite database initialization and connection management."""

import json
import sqlite3
from pathlib import Path
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA_SQL = """
-- 表 1: machines — 機台主檔
CREATE TABLE IF NOT EXISTS machines (
    machine_id   TEXT PRIMARY KEY,
    ip_address   TEXT,
    machine_type TEXT,
    description  TEXT
);

-- 表 2: machine_state — 機台即時狀態（持續追蹤，解決跨日問題）
CREATE TABLE IF NOT EXISTS machine_state (
    machine_id       TEXT PRIMARY KEY,
    current_status   TEXT,
    current_program  TEXT,
    current_tool     TEXT,
    current_diameter REAL,
    last_start_time  TEXT,
    last_update      TEXT,
    parse_offsets    TEXT,
    FOREIGN KEY (machine_id) REFERENCES machines(machine_id)
);

-- 表 3: state_events — 狀態變化事件（稼動率核心）
CREATE TABLE IF NOT EXISTS state_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id   TEXT NOT NULL,
    event_time   TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    source       TEXT NOT NULL,
    program_name TEXT,
    FOREIGN KEY (machine_id) REFERENCES machines(machine_id)
);
CREATE INDEX IF NOT EXISTS idx_state_events_time
    ON state_events(machine_id, event_time);

-- 表 4: program_loads — 程式載入事件
CREATE TABLE IF NOT EXISTS program_loads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id   TEXT NOT NULL,
    load_time    TEXT NOT NULL,
    program_path TEXT NOT NULL,
    program_name TEXT NOT NULL,
    work_order   TEXT,
    side         TEXT,
    m98p_calls   TEXT,
    FOREIGN KEY (machine_id) REFERENCES machines(machine_id)
);

-- 表 5: tool_changes — 換刀事件
CREATE TABLE IF NOT EXISTS tool_changes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id   TEXT NOT NULL,
    change_time  TEXT NOT NULL,
    station      INTEGER NOT NULL,
    block        INTEGER,
    program_name TEXT,
    FOREIGN KEY (machine_id) REFERENCES machines(machine_id)
);

-- 表 6: nc_table_boards — NC表板別對照（用戶上傳）
CREATE TABLE IF NOT EXISTS nc_table_boards (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    work_order     TEXT NOT NULL,
    side           TEXT NOT NULL,
    sub_program    INTEGER NOT NULL,
    board_name     TEXT NOT NULL,
    hole_count     INTEGER,
    description    TEXT
);
CREATE INDEX IF NOT EXISTS idx_nc_boards
    ON nc_table_boards(work_order, side, sub_program);

-- 表 7: utilization_hourly — 小時稼動率（預計算）
CREATE TABLE IF NOT EXISTS utilization_hourly (
    machine_id   TEXT NOT NULL,
    hour_start   TEXT NOT NULL,
    run_seconds  INTEGER NOT NULL,
    total_seconds INTEGER NOT NULL,
    utilization  REAL NOT NULL,
    program_name TEXT,
    PRIMARY KEY (machine_id, hour_start)
);

-- 表 8: alarms — 報警事件
CREATE TABLE IF NOT EXISTS alarms (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id   TEXT NOT NULL,
    alarm_time   TEXT NOT NULL,
    alarm_code   INTEGER,
    description  TEXT,
    FOREIGN KEY (machine_id) REFERENCES machines(machine_id)
);
"""


def init_db(db_path: Path = None) -> None:
    """Create database and all tables."""
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA_SQL)
    conn.close()


@contextmanager
def get_conn(db_path: Path = None):
    """Context manager for database connections."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_machine(machine_id: str, ip_address: str = None,
                 machine_type: str = None, db_path: Path = None) -> None:
    """Initialize a machine record and its state."""
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO machines (machine_id, ip_address, machine_type) "
            "VALUES (?, ?, ?)",
            (machine_id, ip_address, machine_type),
        )
        conn.execute(
            "INSERT OR IGNORE INTO machine_state (machine_id, parse_offsets) "
            "VALUES (?, ?)",
            (machine_id, json.dumps({})),
        )


def get_parse_offsets(machine_id: str, date_str: str = None,
                      db_path: Path = None) -> dict:
    """Get current parse offsets for a machine.

    Offsets are stored as {date_str: {log_type: offset}}.
    If date_str is given, returns offsets for that date only.
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT parse_offsets FROM machine_state WHERE machine_id = ?",
            (machine_id,),
        ).fetchone()
        if row and row["parse_offsets"]:
            all_offsets = json.loads(row["parse_offsets"])
        else:
            all_offsets = {}

    if date_str:
        return all_offsets.get(date_str, {})
    return all_offsets


def update_parse_offset(machine_id: str, log_type: str, offset: int,
                        date_str: str = None, db_path: Path = None) -> None:
    """Update parse offset for a specific log type and date."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT parse_offsets FROM machine_state WHERE machine_id = ?",
            (machine_id,),
        ).fetchone()
        all_offsets = json.loads(row["parse_offsets"]) if row and row["parse_offsets"] else {}

    key = date_str or "_current"
    if key not in all_offsets:
        all_offsets[key] = {}
    all_offsets[key][log_type] = offset

    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE machine_state SET parse_offsets = ? WHERE machine_id = ?",
            (json.dumps(all_offsets), machine_id),
        )


def update_machine_state(machine_id: str, db_path: Path = None, **kwargs) -> None:
    """Update machine state fields.

    Accepts keyword arguments matching machine_state columns:
    current_status, current_program, current_tool, current_diameter,
    last_start_time, last_update.
    """
    valid_fields = {
        "current_status", "current_program", "current_tool",
        "current_diameter", "last_start_time", "last_update",
    }
    updates = {k: v for k, v in kwargs.items() if k in valid_fields}
    if not updates:
        return
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [machine_id]
    with get_conn(db_path) as conn:
        conn.execute(
            f"UPDATE machine_state SET {set_clause} WHERE machine_id = ?",
            values,
        )


def get_machine_state(machine_id: str, db_path: Path = None) -> dict:
    """Get current machine state."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM machine_state WHERE machine_id = ?",
            (machine_id,),
        ).fetchone()
        return dict(row) if row else {}

"""TARN.Log parser — precise start/stop/reset/tool-change events (ms precision).

TARN.Log is CP932 encoded and contains discrete events:
  起動-------(MB300700)        → START
  停止-------(MB300721)        → STOP
  リセット---(MB300740)        → RESET
  異常リセット-(MB300940)      → ABNORMAL_RESET
  ToolChenge→ST:[nn] BLOCK:[n] → tool change
"""

import re
from pathlib import Path
from datetime import datetime

from .db import get_conn, update_parse_offset, update_machine_state

# Event patterns (applied to CP932-decoded text)
PATTERNS = {
    "START": re.compile(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) 起動.*MB300700"),
    "STOP": re.compile(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) 停止.*MB300721"),
    "RESET": re.compile(
        r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) リセット---.*MB300740"
    ),
    "ABNORMAL_RESET": re.compile(
        r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) 異常リセット.*MB300940"
    ),
}

TOOL_CHANGE_PATTERN = re.compile(
    r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) "
    r"ToolChenge→ST:\[(\d+)\] BLOCK:\[(\d+)\]"
)


def parse_timestamp(ts_str: str) -> str:
    """Convert '2026/03/17 00:05:08.090' to ISO8601 '2026-03-17T00:05:08.090'."""
    return ts_str.replace("/", "-", 2).replace(" ", "T", 1)


def parse_tarn(file_path: Path, machine_id: str, byte_offset: int = 0,
               db_path: Path = None, date_str: str = None) -> int:
    """Parse TARN.Log incrementally from byte_offset.

    Returns new byte offset after parsing.
    """
    if not file_path.exists():
        return byte_offset

    with open(file_path, "rb") as f:
        f.seek(byte_offset)
        raw = f.read()
        new_offset = byte_offset + len(raw)

    if not raw:
        return byte_offset

    text = raw.decode("cp932", errors="replace")
    lines = text.splitlines()

    state_events = []
    tool_events = []

    # Get current program from machine state
    from .db import get_machine_state
    state = get_machine_state(machine_id, db_path)
    current_program = state.get("current_program")

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Check state events
        for event_type, pattern in PATTERNS.items():
            m = pattern.match(line)
            if m:
                ts = parse_timestamp(m.group(1))
                state_events.append((machine_id, ts, event_type, "TARN", current_program))
                break

        # Check tool change
        m = TOOL_CHANGE_PATTERN.match(line)
        if m:
            ts = parse_timestamp(m.group(1))
            station = int(m.group(2))
            block = int(m.group(3))
            tool_events.append((machine_id, ts, station, block, current_program))

    # Write to database
    with get_conn(db_path) as conn:
        if state_events:
            conn.executemany(
                "INSERT INTO state_events (machine_id, event_time, event_type, source, program_name) "
                "VALUES (?, ?, ?, ?, ?)",
                state_events,
            )
        if tool_events:
            conn.executemany(
                "INSERT INTO tool_changes (machine_id, change_time, station, block, program_name) "
                "VALUES (?, ?, ?, ?, ?)",
                tool_events,
            )

    # Update parse offset
    update_parse_offset(machine_id, "TARN.Log", new_offset, date_str, db_path)

    # Update machine state with last event
    if state_events:
        last_type = state_events[-1][2]
        last_time = state_events[-1][1]
        status_map = {
            "START": "RUN",
            "STOP": "STOP",
            "RESET": "RESET",
            "ABNORMAL_RESET": "RESET",
        }
        updates = {
            "current_status": status_map.get(last_type, last_type),
            "last_update": last_time,
        }
        if last_type == "START":
            updates["last_start_time"] = last_time
        update_machine_state(machine_id, db_path, **updates)

    return new_offset

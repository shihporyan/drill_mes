"""Drive.Log parser — per-second state recording, extract state changes only.

Drive.Log is ASCII/latin-1 encoded, CSV format, ~67K lines/day.
Only state transitions (Col4 changes) are written to the database.

Format: date,time,mode,status,program,X,Y,tool,diameter,msg_code,...
Col:     0     1     2    3      4    5  6   7      8       9
"""

import re
from pathlib import Path

from .db import get_conn, update_parse_offset, update_machine_state

# Map Drive.Log status values to event types
STATUS_TO_EVENT = {
    "RUN": "START",
    "STOP": "STOP",
    "RESET": "RESET",
}


def parse_timestamp(date_str: str, time_str: str) -> str:
    """Convert '2026/03/17','00:05:08' to ISO8601 '2026-03-17T00:05:08'."""
    return date_str.replace("/", "-") + "T" + time_str


def parse_drive(file_path: Path, machine_id: str, byte_offset: int = 0,
                db_path: Path = None, date_str: str = None) -> int:
    """Parse Drive.Log incrementally, extracting only state changes.

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

    text = raw.decode("latin-1", errors="replace")
    lines = text.splitlines()

    # Get previous state to detect changes
    from .db import get_machine_state
    state = get_machine_state(machine_id, db_path)
    prev_status = state.get("current_status")
    current_program = state.get("current_program")

    state_events = []
    last_tool = None
    last_diameter = None
    last_status = prev_status

    for line in lines:
        line = line.strip()
        if not line:
            continue

        fields = line.split(",")
        if len(fields) < 10:
            continue

        date_str = fields[0].strip()
        time_str = fields[1].strip()
        status = fields[3].strip()
        tool = fields[7].strip()
        diameter = fields[8].strip()

        # Only record state changes
        if status != last_status and status in STATUS_TO_EVENT:
            ts = parse_timestamp(date_str, time_str)
            event_type = STATUS_TO_EVENT[status]
            state_events.append((machine_id, ts, event_type, "DRIVE", current_program))

        last_status = status
        last_tool = tool
        try:
            last_diameter = float(diameter)
        except (ValueError, TypeError):
            pass

    # Write to database
    with get_conn(db_path) as conn:
        if state_events:
            conn.executemany(
                "INSERT INTO state_events (machine_id, event_time, event_type, source, program_name) "
                "VALUES (?, ?, ?, ?, ?)",
                state_events,
            )

    # Update parse offset
    update_parse_offset(machine_id, "Drive.Log", new_offset, date_str, db_path)

    # Update machine state
    updates = {"last_update": parse_timestamp(fields[0].strip(), fields[1].strip())} if lines and len(lines[-1].split(",")) >= 2 else {}
    if last_status:
        updates["current_status"] = last_status
    if last_tool:
        updates["current_tool"] = last_tool
    if last_diameter is not None:
        updates["current_diameter"] = last_diameter
    if updates:
        update_machine_state(machine_id, db_path, **updates)

    return new_offset

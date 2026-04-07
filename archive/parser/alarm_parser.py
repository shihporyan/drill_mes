"""Alarm.Log parser — alarm events.

Alarm.Log is ASCII/latin-1 encoded, CSV format.
Format: date,time,alarm_code,0,0,0,0,0,0
"""

from pathlib import Path

from .db import get_conn, update_parse_offset


def parse_timestamp(date_str: str, time_str: str) -> str:
    """Convert '2026/03/17','08:10:59' to ISO8601."""
    return date_str.replace("/", "-") + "T" + time_str


def parse_alarm(file_path: Path, machine_id: str, byte_offset: int = 0,
                db_path: Path = None, date_str: str = None) -> int:
    """Parse Alarm.Log incrementally from byte_offset.

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

    alarm_events = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        fields = line.split(",")
        if len(fields) < 3:
            continue

        date_str = fields[0].strip()
        time_str = fields[1].strip()
        try:
            alarm_code = int(fields[2].strip())
        except (ValueError, TypeError):
            continue

        ts = parse_timestamp(date_str, time_str)
        alarm_events.append((machine_id, ts, alarm_code, None))

    with get_conn(db_path) as conn:
        if alarm_events:
            conn.executemany(
                "INSERT INTO alarms (machine_id, alarm_time, alarm_code, description) "
                "VALUES (?, ?, ?, ?)",
                alarm_events,
            )

    update_parse_offset(machine_id, "Alarm.Log", new_offset, date_str, db_path)
    return new_offset

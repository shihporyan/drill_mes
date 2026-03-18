"""Utilization calculator — compute hourly utilization from state_events.

Uses TARN.Log events (ms precision) as primary source.
Falls back to Drive.Log events if TARN data is missing.
"""

from datetime import datetime, timedelta
from pathlib import Path

from .db import get_conn


def calculate_utilization(machine_id: str, date_str: str,
                          db_path: Path = None) -> list:
    """Calculate hourly utilization for a given date.

    Args:
        machine_id: e.g. 'DRILL-01'
        date_str: e.g. '20260317' (YYYYMMDD)

    Returns:
        List of dicts: [{hour_start, run_seconds, total_seconds, utilization, program_name}, ...]
    """
    date_iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    day_start = f"{date_iso}T00:00:00"
    day_end = f"{date_iso}T23:59:59.999"

    with get_conn(db_path) as conn:
        # Get all state events for this day, preferring TARN source
        rows = conn.execute(
            "SELECT event_time, event_type, source, program_name "
            "FROM state_events "
            "WHERE machine_id = ? AND event_time >= ? AND event_time <= ? "
            "ORDER BY event_time",
            (machine_id, day_start, day_end),
        ).fetchall()

    if not rows:
        return []

    # Use all events sorted by time. When TARN and DRIVE report the same
    # transition within 2 seconds, keep TARN (higher precision).
    events = [dict(r) for r in rows]
    deduped = []
    for ev in events:
        if ev["source"] == "DRIVE" and deduped:
            prev = deduped[-1]
            if (prev["source"] == "TARN"
                    and prev["event_type"] == ev["event_type"]
                    and abs(len(prev["event_time"]) - len(ev["event_time"])) >= 0
                    and prev["event_time"][:19] == ev["event_time"][:19]):
                continue  # skip duplicate DRIVE event
        deduped.append(ev)
    events = deduped

    # Build RUN intervals
    run_intervals = []
    current_start = None
    current_program = None

    # Check if machine was already running at start of day
    # (look for the last event before this day)
    with get_conn(db_path) as conn:
        prev_event = conn.execute(
            "SELECT event_time, event_type, program_name FROM state_events "
            "WHERE machine_id = ? AND event_time < ? "
            "ORDER BY event_time DESC LIMIT 1",
            (machine_id, day_start),
        ).fetchone()

    if prev_event and prev_event["event_type"] == "START":
        current_start = day_start
        current_program = prev_event["program_name"]

    for event in events:
        et = event["event_type"] if isinstance(event, dict) else event["event_type"]
        event_time = event["event_time"] if isinstance(event, dict) else event["event_time"]
        prog = event["program_name"] if isinstance(event, dict) else event["program_name"]

        if et == "START":
            current_start = event_time
            current_program = prog
        elif et in ("STOP", "RESET", "ABNORMAL_RESET"):
            if current_start:
                run_intervals.append((current_start, event_time, current_program))
                current_start = None

    # If still running at end of day
    if current_start:
        run_intervals.append((current_start, day_end, current_program))

    # Calculate hourly utilization
    hourly = {}
    for hour in range(24):
        hour_start_dt = datetime.fromisoformat(f"{date_iso}T{hour:02d}:00:00")
        hour_end_dt = hour_start_dt + timedelta(hours=1)
        hour_start_str = hour_start_dt.isoformat()
        hour_end_str = hour_end_dt.isoformat()

        run_secs = 0.0
        hour_program = None

        for istart, iend, prog in run_intervals:
            # Calculate overlap between run interval and this hour
            overlap_start = max(istart, hour_start_str)
            overlap_end = min(iend, hour_end_str)

            if overlap_start < overlap_end:
                # Parse timestamps to compute duration
                try:
                    t1 = datetime.fromisoformat(overlap_start)
                    t2 = datetime.fromisoformat(overlap_end)
                    run_secs += (t2 - t1).total_seconds()
                    if prog:
                        hour_program = prog
                except ValueError:
                    pass

        total_secs = 3600
        # Last hour of the day: check if we have data up to this point
        utilization = run_secs / total_secs if total_secs > 0 else 0.0

        hourly[hour] = {
            "hour_start": hour_start_str,
            "run_seconds": int(run_secs),
            "total_seconds": total_secs,
            "utilization": round(utilization, 4),
            "program_name": hour_program,
        }

    return list(hourly.values())


def save_utilization(machine_id: str, date_str: str,
                     db_path: Path = None) -> list:
    """Calculate and save hourly utilization to database.

    Returns the calculated utilization data.
    """
    results = calculate_utilization(machine_id, date_str, db_path)

    with get_conn(db_path) as conn:
        for r in results:
            if r["run_seconds"] > 0 or r["utilization"] > 0:
                conn.execute(
                    "INSERT OR REPLACE INTO utilization_hourly "
                    "(machine_id, hour_start, run_seconds, total_seconds, utilization, program_name) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (machine_id, r["hour_start"], r["run_seconds"],
                     r["total_seconds"], r["utilization"], r["program_name"]),
                )

    return results

"""
Takeuchi Drive.Log parser.

Parses CSV log files from Takeuchi drilling machines, computing:
- Hourly utilization (RUN/RESET/STOP seconds)
- Hole counts (col10 counter delta)
- State transitions
- Machine current state

Supports incremental parsing via parse_progress table.
Handles cross-midnight rows, counter resets, and file overwrites.

Usage:
    python parsers/drive_log_parser.py
"""

import csv
import datetime
import io
import logging
import os
import sqlite3
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from parsers.base_parser import (
    load_machines_config,
    load_settings,
    get_enabled_machines,
    get_db_path,
    get_db_connection,
    check_file_overwrite,
    get_parse_progress,
    update_parse_progress,
    check_db_archive,
)

logger = logging.getLogger(__name__)

VALID_STATES = {"RUN", "RESET", "STOP"}


def parse_csv_line(line):
    """Parse a single Drive.Log CSV line into a structured dict.

    Args:
        line: Raw CSV line string.

    Returns:
        dict with keys: date, time, datetime, mode, state, program,
             tool_num, drill_dia, counter, hour
        None if line is malformed or has invalid state.
    """
    try:
        reader = csv.reader(io.StringIO(line))
        fields = next(reader)

        if len(fields) < 23:
            return None

        # Strip all fields (handle leading spaces like "   630.000")
        fields = [f.strip() for f in fields]

        date_str = fields[0]    # e.g. "2026/04/02"
        time_str = fields[1]    # e.g. "10:56:35"
        mode = fields[2]        # AUTO / MAN
        state = fields[3]       # RUN / RESET / STOP

        if state not in VALID_STATES:
            return None

        program = fields[4]
        tool_num = fields[7]

        try:
            drill_dia = float(fields[8])
        except (ValueError, IndexError):
            drill_dia = 0.0

        try:
            counter = int(fields[10])
        except (ValueError, IndexError):
            counter = 0

        # Parse date and time for hour assignment
        try:
            dt = datetime.datetime.strptime(
                "{} {}".format(date_str, time_str), "%Y/%m/%d %H:%M:%S"
            )
        except ValueError:
            return None

        # Convert date to ISO format (YYYY-MM-DD)
        iso_date = dt.strftime("%Y-%m-%d")

        return {
            "date": iso_date,
            "time": time_str,
            "datetime": dt,
            "iso_timestamp": dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "mode": mode,
            "state": state,
            "program": program,
            "tool_num": tool_num,
            "drill_dia": drill_dia,
            "counter": counter,
            "hour": dt.hour,
        }
    except Exception as e:
        logger.debug("Failed to parse line: %s (%s)", line[:80], e)
        return None


def parse_log_file(db_path, machine_id, log_path, day_prefix):
    """Parse a single Drive.Log file and write results to SQLite.

    This is the core function. It:
    1. Checks for file overwrite (size shrink)
    2. Reads from last_line for incremental parsing
    3. Counts RUN/RESET/STOP seconds per hour
    4. Computes hole counts from col10 counter deltas
    5. Detects state transitions
    6. Updates machine_current_state
    7. UPSERTs hourly_utilization
    8. Updates parse_progress

    Args:
        db_path: Path to SQLite database.
        machine_id: Machine identifier (e.g. 'M13').
        log_path: Absolute path to the Drive.Log file.
        day_prefix: Two-digit day string (e.g. '17').
    """
    if not os.path.exists(log_path):
        logger.warning("[%s] Log file not found: %s", machine_id, log_path)
        return

    file_size = os.path.getsize(log_path)
    if file_size == 0:
        logger.debug("[%s] Empty log file: %s", machine_id, log_path)
        return

    with get_db_connection(db_path) as conn:
        # Check for file overwrite (monthly cycle)
        overwritten = check_file_overwrite(conn, machine_id, day_prefix, file_size)

        # Get last parse position
        last_line, _ = get_parse_progress(conn, machine_id, day_prefix)
        if overwritten:
            last_line = 0

        # Read all lines from file
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
        except Exception as e:
            logger.error("[%s] Failed to read %s: %s", machine_id, log_path, e)
            return

        total_lines = len(all_lines)
        if last_line >= total_lines:
            logger.debug("[%s] No new lines (at %d/%d)", machine_id, last_line, total_lines)
            return

        new_lines = all_lines[last_line:]
        logger.info("[%s] Processing lines %d-%d of %s (%d new lines)",
                    machine_id, last_line, total_lines - 1, os.path.basename(log_path), len(new_lines))

        # Parse all new lines
        parsed_rows = []
        skipped = 0
        for line in new_lines:
            line = line.strip()
            if not line:
                skipped += 1
                continue
            row = parse_csv_line(line)
            if row is None:
                skipped += 1
                continue
            parsed_rows.append(row)

        if skipped > 0:
            logger.debug("[%s] Skipped %d unparseable lines", machine_id, skipped)

        if not parsed_rows:
            update_parse_progress(conn, machine_id, day_prefix, total_lines, None, file_size)
            return

        # ---- Aggregate hourly data ----
        # Group by (date, hour)
        # Key: (date_str, hour) -> {run, reset, stop, first_counter, last_counter}
        hourly = {}
        for row in parsed_rows:
            key = (row["date"], row["hour"])
            if key not in hourly:
                hourly[key] = {
                    "run": 0,
                    "reset": 0,
                    "stop": 0,
                    "first_counter": row["counter"],
                    "last_counter": row["counter"],
                    "prev_counter": None,
                    "hole_count": 0,
                }
            bucket = hourly[key]
            state_lower = row["state"].lower()
            if state_lower in ("run", "reset", "stop"):
                bucket[state_lower] += 1  # Each row = 1 second

            # Hole count: track counter, detect resets
            if bucket["prev_counter"] is not None:
                delta = row["counter"] - bucket["prev_counter"]
                if delta > 0:
                    bucket["hole_count"] += delta
                elif delta < 0:
                    # Counter reset detected - start new counting period
                    logger.debug("[%s] Counter reset at %s (from %d to %d)",
                                 machine_id, row["iso_timestamp"],
                                 bucket["prev_counter"], row["counter"])
            bucket["prev_counter"] = row["counter"]
            bucket["last_counter"] = row["counter"]

        # If we're doing incremental parse and this is not the first run,
        # we need to handle cross-boundary counter tracking.
        # For simplicity in incremental mode, hole_count from first_counter
        # to last_counter is used, but we track per-row deltas above.

        # ---- Detect state transitions ----
        transitions = []
        # Get previous state from machine_current_state
        cursor = conn.execute(
            "SELECT state FROM machine_current_state WHERE machine_id=?",
            (machine_id,),
        )
        prev_state_row = cursor.fetchone()
        prev_state = prev_state_row[0] if prev_state_row else None

        for row in parsed_rows:
            if prev_state is not None and row["state"] != prev_state:
                transitions.append({
                    "machine_id": machine_id,
                    "timestamp": row["iso_timestamp"],
                    "from_state": prev_state,
                    "to_state": row["state"],
                    "program": row["program"],
                    "tool_num": row["tool_num"],
                    "drill_dia": row["drill_dia"],
                })
            prev_state = row["state"]

        # ---- Write to database ----

        # If file was overwritten, clear old data for affected dates
        if overwritten:
            dates_in_log = set(r["date"] for r in parsed_rows)
            for d in dates_in_log:
                conn.execute(
                    "DELETE FROM hourly_utilization WHERE machine_id=? AND date=?",
                    (machine_id, d),
                )
                conn.execute(
                    "DELETE FROM state_transitions WHERE machine_id=? AND timestamp LIKE ?",
                    (machine_id, d + "%"),
                )
            conn.commit()

        # UPSERT hourly_utilization
        for (date_str, hour), bucket in hourly.items():
            total = bucket["run"] + bucket["reset"] + bucket["stop"]
            util = (bucket["run"] / total * 100.0) if total > 0 else 0.0
            util = round(util, 1)

            conn.execute(
                "INSERT INTO hourly_utilization "
                "(machine_id, date, hour, run_seconds, reset_seconds, stop_seconds, "
                "total_seconds, utilization, hole_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(machine_id, date, hour) DO UPDATE SET "
                "run_seconds=excluded.run_seconds, "
                "reset_seconds=excluded.reset_seconds, "
                "stop_seconds=excluded.stop_seconds, "
                "total_seconds=excluded.total_seconds, "
                "utilization=excluded.utilization, "
                "hole_count=excluded.hole_count",
                (machine_id, date_str, hour,
                 bucket["run"], bucket["reset"], bucket["stop"],
                 total, util, bucket["hole_count"]),
            )

        # INSERT state transitions
        for t in transitions:
            conn.execute(
                "INSERT INTO state_transitions "
                "(machine_id, timestamp, from_state, to_state, program, tool_num, drill_dia) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (t["machine_id"], t["timestamp"], t["from_state"], t["to_state"],
                 t["program"], t["tool_num"], t["drill_dia"]),
            )

        # Update machine_current_state with last row
        last_row = parsed_rows[-1]
        # Determine "since" - find when current state started
        since = last_row["iso_timestamp"]
        for i in range(len(parsed_rows) - 1, -1, -1):
            if parsed_rows[i]["state"] != last_row["state"]:
                if i + 1 < len(parsed_rows):
                    since = parsed_rows[i + 1]["iso_timestamp"]
                break
        else:
            # All rows have same state, use first row
            since = parsed_rows[0]["iso_timestamp"]

        conn.execute(
            "INSERT INTO machine_current_state "
            "(machine_id, state, mode, program, tool_num, drill_dia, since, last_update, counter) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(machine_id) DO UPDATE SET "
            "state=excluded.state, mode=excluded.mode, program=excluded.program, "
            "tool_num=excluded.tool_num, drill_dia=excluded.drill_dia, "
            "since=excluded.since, last_update=excluded.last_update, counter=excluded.counter",
            (machine_id, last_row["state"], last_row["mode"], last_row["program"],
             last_row["tool_num"], last_row["drill_dia"], since,
             last_row["iso_timestamp"], last_row["counter"]),
        )

        # Update parse progress
        update_parse_progress(
            conn, machine_id, day_prefix, total_lines,
            last_row["iso_timestamp"], file_size,
        )

        conn.commit()

        logger.info(
            "[%s] Done: %d rows parsed, %d hours updated, %d transitions recorded",
            machine_id, len(parsed_rows), len(hourly), len(transitions),
        )


def get_log_path(settings, machine_id, day_prefix):
    """Build the local backup log file path.

    Args:
        settings: Settings dict with backup_root.
        machine_id: Machine identifier.
        day_prefix: Two-digit day string.

    Returns:
        str: Full path to the Drive.Log file.
    """
    today = datetime.date.today()
    date_dir = today.strftime("%Y%m%d")
    return os.path.join(
        settings["backup_root"], machine_id, date_dir,
        "{}Drive.Log".format(day_prefix),
    )


def run_parser_cycle(db_path=None, settings=None, machines_config=None):
    """Execute one full parse cycle for all enabled machines.

    Args:
        db_path: Optional database path override.
        settings: Optional settings dict override.
        machines_config: Optional machines config override.
    """
    if settings is None:
        settings = load_settings()
    if machines_config is None:
        machines_config = load_machines_config()
    if db_path is None:
        db_path = get_db_path(settings)

    enabled = get_enabled_machines(machines_config)
    if not enabled:
        logger.warning("No enabled machines found in config.")
        return

    today = datetime.date.today()
    day_prefix = today.strftime("%d")

    logger.info("Parser cycle start: %d enabled machines, day_prefix=%s", len(enabled), day_prefix)

    for machine in enabled:
        machine_id = machine["id"]
        log_path = get_log_path(settings, machine_id, day_prefix)

        try:
            parse_log_file(db_path, machine_id, log_path, day_prefix)
        except Exception as e:
            logger.error("[%s] Parser error: %s", machine_id, e, exc_info=True)

    # Check if archiving is needed
    try:
        check_db_archive(settings)
    except Exception as e:
        logger.error("Archive check error: %s", e, exc_info=True)

    logger.info("Parser cycle complete.")


def run_parser_loop(interval=None):
    """Run parser in a continuous loop.

    Args:
        interval: Seconds between cycles. Reads from settings if None.
    """
    settings = load_settings()
    if interval is None:
        interval = settings.get("poll_interval_seconds", 600)

    logger.info("Starting parser loop (interval=%ds)", interval)

    while True:
        try:
            run_parser_cycle(settings=settings)
        except Exception as e:
            logger.error("Parser cycle failed: %s", e, exc_info=True)
        logger.info("Next cycle in %d seconds...", interval)
        time.sleep(interval)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if len(sys.argv) > 1 and sys.argv[1] == "--loop":
        run_parser_loop()
    elif len(sys.argv) > 1 and sys.argv[1] == "--once":
        run_parser_cycle()
    else:
        print("Usage:")
        print("  python parsers/drive_log_parser.py --once   # Run one cycle")
        print("  python parsers/drive_log_parser.py --loop   # Run continuously")
        print("")
        print("Testing single file parse:")
        print("  Import and call parse_log_file(db_path, machine_id, log_path, day_prefix)")

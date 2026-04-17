"""
Takeuchi TX1.Log parser — work order tracking via FILEOPERATION LOAD events.

TX1.Log is CP932 encoded and contains operator actions. The key event:
    YYYY/MM/DD HH:MM:SS.mmm OpeLog : FILEOPERATION SCREEN:[PROGRAMLIST] OPERATION:[LOAD] NAME:[O2604031.B]

Work order inference: the last production program loaded (matching O/GR pattern)
is the active work order. O100.txt loads are ignored (main program, not a WO).

Updates machine_current_state.work_order and work_order_side only.
Does not touch state/mode/program/counter (managed by Drive.Log parser).

Supports incremental parsing via parse_progress table with key "tx1_{day_prefix}".
"""

import datetime
import logging
import os
import re
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from parsers.base_parser import (
    load_machines_config,
    load_settings,
    get_machines_by_type,
    get_backup_root,
    get_db_path,
    get_db_connection,
    check_file_overwrite,
    get_parse_progress,
    update_parse_progress,
)
from parsers.drive_log_parser import extract_work_order

logger = logging.getLogger(__name__)

FILEOPERATION_LOAD_PATTERN = re.compile(
    r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d{3})"
    r".*FILEOPERATION.*OPERATION:\[LOAD\] NAME:\[(.+?)\]"
)


def parse_fileoperation_line(line):
    """Extract timestamp and program name from a FILEOPERATION LOAD line.

    Args:
        line: Raw text line from TX1.Log.

    Returns:
        dict with 'timestamp' (ISO str) and 'program_name', or None.
    """
    m = FILEOPERATION_LOAD_PATTERN.match(line)
    if not m:
        return None
    ts_raw = m.group(1)
    # Convert '2026/04/10 21:39:48.796' to '2026-04-10T21:39:48.796'
    iso_ts = ts_raw.replace("/", "-", 2).replace(" ", "T", 1)
    program_name = m.group(2).strip()
    return {"timestamp": iso_ts, "program_name": program_name}


def parse_tx1_file(db_path, machine_id, log_path, day_prefix, reference_date=None):
    """Parse TX1.Log incrementally and update work order in machine_current_state.

    Args:
        db_path: Path to SQLite database.
        machine_id: Machine identifier (e.g. 'M13').
        log_path: Full path to the TX1.Log file.
        day_prefix: Two-digit day string (e.g. '10').
        reference_date: Expected date for this file's events. Events more than
            2 days before this date are skipped (stale previous-month data).
            Defaults to today.
    """
    if not os.path.exists(log_path):
        return

    file_size = os.path.getsize(log_path)
    if file_size == 0:
        return

    progress_key = "tx1_{}".format(day_prefix)
    conn = get_db_connection(db_path)
    try:
        # Check for file overwrite (monthly cycle)
        overwritten = check_file_overwrite(conn, machine_id, progress_key, file_size)

        last_line, _ = get_parse_progress(conn, machine_id, progress_key)

        # Read file with CP932 encoding
        with open(log_path, "r", encoding="cp932", errors="replace") as f:
            all_lines = f.readlines()

        total_lines = len(all_lines)
        if last_line >= total_lines and not overwritten:
            return  # Nothing new to parse

        new_lines = all_lines[last_line:]
        if not new_lines:
            return

        # Extract FILEOPERATION LOAD events
        events = []
        for line in new_lines:
            event = parse_fileoperation_line(line.strip())
            if event:
                events.append(event)

        # Find last production program in new events.
        # Filter out events from previous months — TX1.Log files are
        # monthly-rotating ({DD}TX1.Log), so at midnight the file for
        # today's day_prefix may still contain last month's data until
        # the machine overwrites it.
        ref_date = reference_date or datetime.date.today()
        last_wo = None
        last_side = None
        last_ts = None
        for event in events:
            try:
                event_date = datetime.datetime.fromisoformat(
                    event["timestamp"].split(".")[0]
                ).date()
                if (ref_date - event_date).days > 2:
                    continue  # Skip stale previous-month data
            except (ValueError, TypeError):
                continue
            wo, side = extract_work_order(event["program_name"])
            if wo:
                last_wo = wo
                last_side = side
                last_ts = event["timestamp"]

        # Update machine_current_state with work order (targeted UPDATE only)
        if last_wo is not None:
            conn.execute(
                "UPDATE machine_current_state SET work_order=?, work_order_side=? "
                "WHERE machine_id=?",
                (last_wo, last_side, machine_id),
            )
            conn.commit()
            logger.info("[%s] TX1 work order: %s.%s (at %s)", machine_id, last_wo, last_side, last_ts)

        # Update parse progress
        last_event_ts = last_ts
        if not last_event_ts and events:
            last_event_ts = events[-1]["timestamp"]
        update_parse_progress(conn, machine_id, progress_key, total_lines, last_event_ts, file_size)

    finally:
        conn.close()


def backfill_work_order(db_path, machine_id, backup_root, max_days_back=7):
    """Recover a missing work_order by scanning recent backup TX1.Log files.

    Only acts when the machine's work_order is currently NULL in
    machine_current_state.  This handles the case where the DB was
    recreated or the system started fresh while the last production
    program LOAD event is more than one day old (beyond the reach of
    the normal today + yesterday parse path).

    Scans today's backup folder for older day_prefix files. Because
    robocopy copies the entire LOG directory each cycle, today's
    folder contains the freshest copy of every `{DD}TX1.Log` file
    (including older days that were closed out at their month-end).
    Iterates day_prefix from today backward through `max_days_back`
    days; stops at the first file that has a production LOAD event.

    All I/O is against local backup files; this function never touches
    the machine control computer's SMB share.

    Args:
        db_path: Path to SQLite database.
        machine_id: Machine identifier (e.g. 'M13').
        backup_root: Absolute path to the backup root directory.
        max_days_back: How many days to scan backward (default 7).
    """
    conn = get_db_connection(db_path)
    try:
        cursor = conn.execute(
            "SELECT work_order FROM machine_current_state WHERE machine_id=?",
            (machine_id,),
        )
        row = cursor.fetchone()
        if not row:
            return  # No current_state row yet (Drive.Log parser hasn't run)
        if row[0]:
            return  # Already have a work_order — nothing to backfill

        today = datetime.date.today()
        today_dir = today.strftime("%Y%m%d")
        for days_back in range(max_days_back + 1):
            target_date = today - datetime.timedelta(days=days_back)
            day_prefix = target_date.strftime("%d")
            # Always read from today's backup folder — robocopy keeps the
            # freshest copy of every older day's log there.
            log_path = os.path.join(
                backup_root, machine_id, today_dir,
                "{}TX1.Log".format(day_prefix),
            )

            if not os.path.exists(log_path):
                continue

            last_wo = None
            last_side = None
            last_ts = None
            try:
                with open(log_path, "r", encoding="cp932", errors="replace") as f:
                    for line in f:
                        event = parse_fileoperation_line(line.strip())
                        if event:
                            # Skip events from previous months (stale file)
                            try:
                                event_date = datetime.datetime.fromisoformat(
                                    event["timestamp"].split(".")[0]
                                ).date()
                                if (today - event_date).days > max_days_back + 2:
                                    continue
                            except (ValueError, TypeError):
                                continue
                            wo, side = extract_work_order(event["program_name"])
                            if wo:
                                last_wo = wo
                                last_side = side
                                last_ts = event["timestamp"]
            except Exception as e:
                logger.error(
                    "[%s] TX1 backfill read error (%s): %s",
                    machine_id, log_path, e,
                )
                continue

            if last_wo:
                conn.execute(
                    "UPDATE machine_current_state SET work_order=?, work_order_side=? "
                    "WHERE machine_id=?",
                    (last_wo, last_side, machine_id),
                )
                conn.commit()
                logger.info(
                    "[%s] TX1 backfill: recovered work order %s.%s from %s (at %s)",
                    machine_id, last_wo, last_side, os.path.basename(log_path), last_ts,
                )
                return

        logger.info(
            "[%s] TX1 backfill: no production work order found in last %d days",
            machine_id, max_days_back,
        )
    finally:
        conn.close()


def get_tx1_log_path(settings, machine_id, day_prefix):
    """Build the local TX1.Log file path.

    Args:
        settings: Settings dict with backup_root.
        machine_id: Machine identifier.
        day_prefix: Two-digit day string.

    Returns:
        str: Full path to the TX1.Log file.
    """
    today = datetime.date.today()
    date_dir = today.strftime("%Y%m%d")
    return os.path.join(
        get_backup_root(settings), machine_id, date_dir,
        "{}TX1.Log".format(day_prefix),
    )


def run_parser_cycle(db_path=None, settings=None, machines_config=None):
    """Execute one TX1.Log parse cycle for all enabled Takeuchi machines.

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

    takeuchi_machines = get_machines_by_type(machines_config, "takeuchi")
    if not takeuchi_machines:
        return

    today = datetime.date.today()
    day_prefix = today.strftime("%d")
    date_dir = today.strftime("%Y%m%d")
    backup_root = get_backup_root(settings)

    # Yesterday's day_prefix — today's backup folder has a fresher copy of
    # yesterday's TX1.Log (robocopy copies today's + yesterday's files each cycle).
    yesterday = today - datetime.timedelta(days=1)
    yesterday_prefix = yesterday.strftime("%d")

    logger.info("TX1 parser cycle start: %d Takeuchi machines, day_prefix=%s",
                len(takeuchi_machines), day_prefix)

    for machine in takeuchi_machines:
        machine_id = machine["id"]

        # Parse yesterday's TX1.Log from today's backup (catches late-night events)
        yesterday_path = os.path.join(
            backup_root, machine_id, date_dir,
            "{}TX1.Log".format(yesterday_prefix),
        )
        yesterday_progress_key = "tx1_{}".format(yesterday_prefix)
        try:
            parse_tx1_file(db_path, machine_id, yesterday_path, yesterday_prefix,
                           reference_date=yesterday)
        except Exception as e:
            logger.error("[%s] TX1 parser error (prev-day): %s", machine_id, e, exc_info=True)

        # Parse today's TX1.Log
        log_path = get_tx1_log_path(settings, machine_id, day_prefix)
        try:
            parse_tx1_file(db_path, machine_id, log_path, day_prefix,
                           reference_date=today)
        except Exception as e:
            logger.error("[%s] TX1 parser error: %s", machine_id, e, exc_info=True)

        # If work_order is still NULL after normal parsing, scan further
        # back in the backup folders to recover it (DB recreation safety net).
        try:
            backfill_work_order(db_path, machine_id, backup_root)
        except Exception as e:
            logger.error("[%s] TX1 backfill error: %s", machine_id, e, exc_info=True)

    logger.info("TX1 parser cycle complete.")

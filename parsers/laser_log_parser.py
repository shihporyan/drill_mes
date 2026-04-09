"""
Kataoka Laser Drill log parser.

Parses multiple log files from Kataoka laser drilling machines:
- ClsLaserCom.log: RUN state detection (auto-run alarm add/delete)
- PhysicalMemory.log: Power-on time detection (heartbeat)
- ClsPLCTrd.log: IDLE/STOP/OFFLINE state detection
- ProcTimeEnd.txt / ProcTimeStart.txt: Work order tracking
- LSR files: Hole count extraction

Supports incremental parsing via parse_progress table.

Usage:
    python parsers/laser_log_parser.py --once
    python parsers/laser_log_parser.py --loop
"""

import csv
import datetime
import glob
import io
import logging
import ntpath
import os
import re
import sqlite3
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from parsers.base_parser import (
    load_machines_config,
    load_settings,
    get_machines_by_type,
    get_backup_root,
    get_db_path,
    get_db_connection,
    get_parse_progress,
    update_parse_progress,
    check_db_archive,
)

logger = logging.getLogger(__name__)

# --- Exact strings for state detection (must not fuzzy-match) ---
AUTO_RUN_START = "自動運転中アラーム追加"
AUTO_RUN_END = "自動運転中アラーム削除"

# State detection strings for ClsPLCTrd
PLC_READY = "SetReady--> PC起動準備完(自動)"
PLC_STOP = "GetStatus--> PLC運転停止"
PLC_POWER_OFF = "ProcAlarm--> アラーム追加(Power OFF)"

# Idle detection in ClsLaserCom
PANEL_WAIT = "パネル操作待ち"

# Timestamp pattern in log lines: YYYY/MM/DD HH:MM:SS:mmm
TS_PATTERN = re.compile(r"(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}:\d{3})")

# Count pattern in LSR files: Count:NNN (case-insensitive)
LSR_COUNT_PATTERN = re.compile(r"Count:(\d+)", re.IGNORECASE)
LSR_ALIGNMENT_PATTERN = re.compile(r"alignment;count", re.IGNORECASE)

# Work order naming rule: only WOs whose name contains "WD" or "GR" get
# hole_count computed from their LSR file. All other WOs (test jobs, SCM etc.)
# are still tracked in laser_work_orders, but with hole_count=0.
def is_production_work_order(wo_name):
    """Return True if this WO name qualifies for hole-count extraction."""
    if not wo_name:
        return False
    return "WD" in wo_name or "GR" in wo_name


def parse_log_timestamp(ts_str):
    """Parse a laser log timestamp string to datetime.

    Format: 'YYYY/MM/DD HH:MM:SS:mmm' (milliseconds separated by colon).

    Args:
        ts_str: Timestamp string like '2026/04/07 09:23:47:993'.

    Returns:
        datetime.datetime or None if parsing fails.
    """
    try:
        # Split off milliseconds (last :mmm)
        parts = ts_str.rsplit(":", 1)
        base = parts[0]  # 'YYYY/MM/DD HH:MM:SS'
        ms = int(parts[1]) if len(parts) > 1 else 0
        dt = datetime.datetime.strptime(base, "%Y/%m/%d %H:%M:%S")
        return dt.replace(microsecond=ms * 1000)
    except (ValueError, IndexError):
        return None


def extract_timestamp_from_line(line):
    """Extract the first timestamp from a log line.

    Args:
        line: A log file line.

    Returns:
        datetime.datetime or None.
    """
    m = TS_PATTERN.search(line)
    if m:
        return parse_log_timestamp(m.group(1))
    return None


def find_log_file(log_dir, date_str, component):
    """Find a log file for a given date and component.

    Production format: {date_str}_{component}.log (e.g. 20260407_ClsLaserCom.log)
    Legacy dev format: *_{date_str}_{component}.txt (e.g. 1448_20260407_ClsLaserCom.txt)

    Supports both .log (production) and .txt (dev) extensions.

    Args:
        log_dir: Directory to search in.
        date_str: Date string like '20260407'.
        component: Component name like 'ClsLaserCom'.

    Returns:
        str: Full path to the found file, or None.
    """
    for ext in ("log", "txt"):
        # Production format: date is the prefix
        pattern = os.path.join(log_dir, "{}_{}*.{}".format(date_str, component, ext))
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
        # Legacy format: optional prefix before date
        pattern = os.path.join(log_dir, "*_{}_{}*.{}".format(date_str, component, ext))
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return None


# =============================================================================
# A. Utilization: ClsLaserCom + PhysicalMemory
# =============================================================================

def parse_cls_laser_com(filepath):
    """Parse ClsLaserCom log to extract RUN intervals.

    Scans for exact matches of AUTO_RUN_START and AUTO_RUN_END. Returns both
    the matched intervals and a possible leading orphan DEL — a DEL that
    appeared before any ADD in the file. A leading orphan means a RUN that
    started the previous day carried over past midnight; callers can use the
    day's pm_first heartbeat to close it on this day.

    Args:
        filepath: Path to ClsLaserCom log file.

    Returns:
        (intervals, leading_orphan_del):
            intervals: list of (datetime, datetime|None) RUN intervals.
                       None end = unclosed interval at file tail.
            leading_orphan_del: datetime of the first DEL seen before any ADD,
                                or None if the file started normally.
    """
    if not filepath or not os.path.exists(filepath):
        return [], None

    intervals = []
    current_start = None
    leading_orphan_del = None
    saw_add = False

    with open(filepath, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            # Check for exact auto-run start
            if AUTO_RUN_START in line and "パネル操作待ち" not in line:
                ts = extract_timestamp_from_line(line)
                if ts:
                    current_start = ts
                    saw_add = True

            # Check for exact auto-run end
            elif AUTO_RUN_END in line:
                ts = extract_timestamp_from_line(line)
                if not ts:
                    continue
                if current_start is not None:
                    intervals.append((current_start, ts))
                    current_start = None
                elif not saw_add and leading_orphan_del is None:
                    # DEL before any ADD in this file: RUN carried over from
                    # the previous day. Record once; subsequent orphan DELs
                    # (if any) are data anomalies and ignored.
                    leading_orphan_del = ts

    # If there's an unmatched start, keep it as open interval (will be closed later)
    if current_start is not None:
        intervals.append((current_start, None))

    return intervals, leading_orphan_del


def parse_physical_memory(filepath):
    """Parse PhysicalMemory log to get power-on time range.

    Args:
        filepath: Path to PhysicalMemory log file.

    Returns:
        tuple: (first_timestamp, last_timestamp) or (None, None).
    """
    if not filepath or not os.path.exists(filepath):
        return None, None

    first_ts = None
    last_ts = None

    with open(filepath, "r", encoding="utf-8-sig") as f:
        for line in f:
            ts = extract_timestamp_from_line(line)
            if ts:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts

    return first_ts, last_ts


def compute_hourly_utilization(run_intervals, power_on_start, power_on_end):
    """Compute hourly utilization buckets from RUN intervals and power-on range.

    Args:
        run_intervals: List of (start_dt, end_dt) RUN intervals.
        power_on_start: datetime of first heartbeat.
        power_on_end: datetime of last heartbeat.

    Returns:
        dict: {(date_str, hour): {"run": secs, "idle": secs, "total": secs, "utilization": pct}}
    """
    if not power_on_start or not power_on_end:
        return {}

    # Close any open intervals with power_on_end
    closed_intervals = []
    for start, end in run_intervals:
        if end is None:
            end = power_on_end
        closed_intervals.append((start, end))

    # Determine the range of hours to process
    start_hour = power_on_start.replace(minute=0, second=0, microsecond=0)
    end_hour = power_on_end.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)

    hourly = {}
    current = start_hour
    while current < end_hour:
        hour_start = current
        hour_end = current + datetime.timedelta(hours=1)

        # Clamp to power-on range
        effective_start = max(hour_start, power_on_start)
        effective_end = min(hour_end, power_on_end)

        if effective_start >= effective_end:
            current = hour_end
            continue

        total_seconds = (effective_end - effective_start).total_seconds()

        # Calculate RUN seconds within this hour
        run_seconds = 0.0
        for interval_start, interval_end in closed_intervals:
            # Overlap with this hour
            overlap_start = max(interval_start, effective_start)
            overlap_end = min(interval_end, effective_end)
            if overlap_start < overlap_end:
                run_seconds += (overlap_end - overlap_start).total_seconds()

        idle_seconds = total_seconds - run_seconds
        utilization = (run_seconds / total_seconds * 100.0) if total_seconds > 0 else 0.0

        date_str = hour_start.strftime("%Y-%m-%d")
        hour_num = hour_start.hour

        hourly[(date_str, hour_num)] = {
            "run": int(run_seconds),
            "idle": int(idle_seconds),
            "total": int(total_seconds),
            "utilization": round(utilization, 1),
            "hole_count": 0,
        }

        current = hour_end

    return hourly


# =============================================================================
# B. Machine State Detection
# =============================================================================

def detect_current_state(laser_com_path, plc_path, pm_first, pm_last):
    """Detect current machine state from latest log events.

    Priority: RUN > IDLE > STOP > OFFLINE

    Args:
        laser_com_path: Path to ClsLaserCom log.
        plc_path: Path to ClsPLCTrd log.
        pm_first: First PhysicalMemory timestamp.
        pm_last: Last PhysicalMemory timestamp.

    Returns:
        tuple: (state, since_timestamp)
            state: 'RUN', 'RESET' (=idle), or 'STOP'
            since: ISO timestamp string
    """
    # Parse ClsLaserCom for last state event
    last_event = None  # ('RUN_START' or 'RUN_END', timestamp)

    if laser_com_path and os.path.exists(laser_com_path):
        with open(laser_com_path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if AUTO_RUN_START in line and "パネル操作待ち" not in line:
                    ts = extract_timestamp_from_line(line)
                    if ts:
                        last_event = ("RUN_START", ts)
                elif AUTO_RUN_END in line:
                    ts = extract_timestamp_from_line(line)
                    if ts:
                        last_event = ("RUN_END", ts)

    # If last event is RUN_START (no matching end), machine is running
    if last_event and last_event[0] == "RUN_START":
        return "RUN", last_event[1].strftime("%Y-%m-%dT%H:%M:%S")

    # Check PLC for stop/idle
    last_plc_event = None
    if plc_path and os.path.exists(plc_path):
        with open(plc_path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if PLC_STOP in line:
                    ts = extract_timestamp_from_line(line)
                    if ts:
                        last_plc_event = ("STOP", ts)
                elif PLC_READY in line:
                    ts = extract_timestamp_from_line(line)
                    if ts:
                        last_plc_event = ("IDLE", ts)
                elif PLC_POWER_OFF in line:
                    ts = extract_timestamp_from_line(line)
                    if ts:
                        last_plc_event = ("OFFLINE", ts)

    # Check for panel wait (idle) in ClsLaserCom
    last_panel_wait = None
    if laser_com_path and os.path.exists(laser_com_path):
        with open(laser_com_path, "r", encoding="utf-8-sig") as f:
            for line in f:
                if PANEL_WAIT in line and AUTO_RUN_START not in line:
                    ts = extract_timestamp_from_line(line)
                    if ts:
                        last_panel_wait = ts

    # Determine state based on most recent event
    candidates = []
    if last_event and last_event[0] == "RUN_END":
        candidates.append(("RESET", last_event[1]))  # RESET = idle after run ends
    if last_plc_event:
        state_map = {"STOP": "STOP", "IDLE": "RESET", "OFFLINE": "STOP"}
        candidates.append((state_map[last_plc_event[0]], last_plc_event[1]))
    if last_panel_wait:
        candidates.append(("RESET", last_panel_wait))

    if candidates:
        # Pick the most recent event
        candidates.sort(key=lambda x: x[1], reverse=True)
        state, since = candidates[0]
        return state, since.strftime("%Y-%m-%dT%H:%M:%S")

    # Default: if we have heartbeat but no state events, consider idle
    if pm_last:
        return "RESET", pm_last.strftime("%Y-%m-%dT%H:%M:%S")

    return "STOP", datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# =============================================================================
# C. Work Order Parsing (ProcTimeEnd / ProcTimeStart)
# =============================================================================

def parse_proc_time_end(filepath):
    """Parse YYYYMM_ProcTimeEnd.txt to extract completed work orders.

    Format: alternating line pairs
    - Time line: "start_time","end_time","duration_secs"
    - Detail line(s): "station","wo_name","lsr_path","machine_type","power"

    One time line can have multiple detail lines (multi-station jobs).

    Args:
        filepath: Path to ProcTimeEnd.txt file.

    Returns:
        list of dicts with keys: start_time, end_time, duration_secs,
            station, work_order, lsr_file_path
    """
    if not filepath or not os.path.exists(filepath):
        return []

    records = []
    current_time_row = None

    with open(filepath, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            # Parse as CSV
            try:
                reader = csv.reader(io.StringIO(line))
                fields = next(reader)
            except Exception:
                continue

            if not fields:
                continue

            # Detect time line: first field looks like a datetime
            if re.match(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}", fields[0]):
                # Time line: "start","end","duration"
                if len(fields) >= 3:
                    current_time_row = {
                        "start_time": fields[0],
                        "end_time": fields[1],
                        "duration_secs": fields[2],
                    }
            elif current_time_row and len(fields) >= 3:
                # Detail line: "station","wo_name","lsr_path"[,"type","power"]
                station = fields[0]
                wo_name = fields[1]
                lsr_path = fields[2] if len(fields) > 2 else ""

                records.append({
                    "start_time": current_time_row["start_time"],
                    "end_time": current_time_row["end_time"],
                    "duration_secs": current_time_row["duration_secs"],
                    "station": station,
                    "work_order": wo_name,
                    "lsr_file_path": lsr_path,
                })

    return records


def parse_proc_time_start(filepath):
    """Parse ProcTimeStart.txt to extract current in-progress work order.

    Format (overwritten each new job):
    - Time line: "station","start_time","end_time","duration_secs"
    - Detail line: "station","wo_name","lsr_path","machine_type","power"

    Args:
        filepath: Path to ProcTimeStart.txt file.

    Returns:
        list of dicts (usually 0 or 1 record).
    """
    if not filepath or not os.path.exists(filepath):
        return []

    records = []
    current_time_row = None

    with open(filepath, "r", encoding="utf-8-sig") as f:
        lines = f.readlines()

    for line in lines:
        line = line.strip()
        if not line:
            continue

        try:
            reader = csv.reader(io.StringIO(line))
            fields = next(reader)
        except Exception:
            continue

        if not fields:
            continue

        # ProcTimeStart has station as first field in time line
        # Detect: first field is a small number (station), second is datetime
        if (len(fields) >= 4 and
                re.match(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}", fields[1])):
            current_time_row = {
                "station": fields[0],
                "start_time": fields[1],
                "end_time": fields[2],
                "duration_secs": fields[3],
            }
        elif current_time_row and len(fields) >= 3:
            station = fields[0]
            wo_name = fields[1]
            lsr_path = fields[2] if len(fields) > 2 else ""

            records.append({
                "start_time": current_time_row["start_time"],
                "end_time": current_time_row["end_time"],
                "duration_secs": current_time_row["duration_secs"],
                "station": station,
                "work_order": wo_name,
                "lsr_file_path": lsr_path,
            })
            current_time_row = None

    return records


# =============================================================================
# D. Hole Count from LSR Files
# =============================================================================

def extract_hole_count_from_lsr(filepath):
    """Extract total hole count from an LSR coordinate file.

    Finds all 'Count:NNN' values in header lines and sums them.
    Skips alignment lines (containing 'alignment;count').

    Args:
        filepath: Path to .lsr file (read as text).

    Returns:
        int: Total hole count, or 0 if file not found.
    """
    if not filepath or not os.path.exists(filepath):
        return 0

    total = 0
    try:
        with open(filepath, "r", encoding="utf-8-sig") as f:
            for line in f:
                # Skip alignment lines
                if LSR_ALIGNMENT_PATTERN.search(line):
                    continue
                # Find Count:NNN patterns
                for m in LSR_COUNT_PATTERN.finditer(line):
                    total += int(m.group(1))
    except Exception as e:
        logger.warning("Failed to read LSR file %s: %s", filepath, e)

    return total


# =============================================================================
# E. Main Parse Function
# =============================================================================

def parse_laser_machine(db_path, machine_id, log_dir, programs_dir, lsr_dir, date_str):
    """Parse all laser log files for one machine and one date, write to DB.

    Args:
        db_path: Path to SQLite database.
        machine_id: Machine identifier (e.g. 'L2').
        log_dir: Directory containing system logs for this date.
        programs_dir: Directory containing ProcTimeEnd/ProcTimeStart files.
        lsr_dir: Directory containing copied LSR files.
        date_str: Date string like '20260407'.
    """
    # Find log files
    laser_com_path = find_log_file(log_dir, date_str, "ClsLaserCom")
    pm_path = find_log_file(log_dir, date_str, "PhysicalMemory")
    plc_path = find_log_file(log_dir, date_str, "ClsPLCTrd")

    if not laser_com_path and not pm_path:
        logger.debug("[%s] No laser logs found for date %s in %s", machine_id, date_str, log_dir)
        return

    logger.info("[%s] Parsing laser logs for date %s", machine_id, date_str)

    # A. Parse utilization
    run_intervals, leading_orphan_del = parse_cls_laser_com(laser_com_path)
    pm_first, pm_last = parse_physical_memory(pm_path)

    # Cross-midnight RUN carryover: if this file opens with a DEL that has no
    # matching ADD, the machine was already running when midnight crossed.
    # Close it using the first heartbeat of today so the leading hours of
    # this day get credited with RUN time.
    if leading_orphan_del is not None and pm_first is not None:
        run_intervals.insert(0, (pm_first, leading_orphan_del))
        logger.info(
            "[%s] Cross-midnight RUN carryover: %s -> %s (from previous day)",
            machine_id,
            pm_first.strftime("%H:%M:%S"),
            leading_orphan_del.strftime("%H:%M:%S"),
        )

    hourly = compute_hourly_utilization(run_intervals, pm_first, pm_last)

    # B. Detect current state
    state, since = detect_current_state(laser_com_path, plc_path, pm_first, pm_last)

    # C. Parse work orders
    # Production uses .log extension, legacy dev uses .txt. Support both.
    wo_records = []
    if programs_dir:
        # Find ProcTimeEnd files (monthly). Collect into a set to deduplicate.
        end_files = set()
        for ext in ("log", "txt"):
            for pattern in ("*_ProcTimeEnd.{}".format(ext), "*ProcTimeEnd.{}".format(ext)):
                end_files.update(glob.glob(os.path.join(programs_dir, "**", pattern), recursive=True))
                end_files.update(glob.glob(os.path.join(programs_dir, pattern)))
        for f in end_files:
            wo_records.extend(parse_proc_time_end(f))

        # Find ProcTimeStart files
        start_files = set()
        for ext in ("log", "txt"):
            pattern = "ProcTimeStart.{}".format(ext)
            start_files.update(glob.glob(os.path.join(programs_dir, "**", pattern), recursive=True))
            direct = os.path.join(programs_dir, pattern)
            if os.path.exists(direct):
                start_files.add(direct)
        for f in start_files:
            wo_records.extend(parse_proc_time_start(f))

    # Deduplicate work orders by (start_time, station)
    seen = set()
    unique_wo = []
    for rec in wo_records:
        key = (rec["start_time"], rec["station"])
        if key not in seen:
            seen.add(key)
            unique_wo.append(rec)

    # D. Extract hole counts from LSR files.
    # Only compute for WOs whose name contains "WD" or "GR"; other WOs (test
    # jobs, SCM experiments, etc.) keep hole_count=0 per product rule.
    # Use ntpath.basename so Windows paths like
    # 'C:\Users\KATAOKA\Desktop\foo.lsr' are split correctly on Mac too.
    for rec in unique_wo:
        rec["hole_count"] = 0
        if not is_production_work_order(rec.get("work_order", "")):
            continue
        lsr_path = rec.get("lsr_file_path", "")
        if not lsr_path or not lsr_dir:
            continue
        lsr_basename = ntpath.basename(lsr_path)
        local_lsr = os.path.join(lsr_dir, lsr_basename)
        if os.path.exists(local_lsr):
            rec["hole_count"] = extract_hole_count_from_lsr(local_lsr)

    # Get the latest work order for machine_current_state
    last_wo = None
    for rec in unique_wo:
        last_wo = rec["work_order"]

    # Attribute each WO's hole_count to the hour of its end_time, so that
    # dashboard "today's holes" (summed from hourly_utilization) reflects real
    # laser drilling output. Only WOs finishing on the day being parsed are
    # counted; WOs from other days belong to their own day's parse run.
    target_iso_date = "{}-{}-{}".format(date_str[:4], date_str[4:6], date_str[6:8])
    for rec in unique_wo:
        if rec.get("hole_count", 0) <= 0:
            continue
        try:
            end_dt = datetime.datetime.strptime(rec["end_time"], "%Y/%m/%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        if end_dt.strftime("%Y-%m-%d") != target_iso_date:
            continue
        key = (target_iso_date, end_dt.hour)
        if key in hourly:
            hourly[key]["hole_count"] += rec["hole_count"]

    # ---- Write to database ----
    with get_db_connection(db_path) as conn:
        # UPSERT hourly_utilization
        for (date_key, hour), bucket in hourly.items():
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
                (machine_id, date_key, hour,
                 bucket["run"], bucket["idle"], 0,
                 bucket["total"], bucket["utilization"],
                 bucket.get("hole_count", 0)),
            )

        # Update machine_current_state
        iso_date = "{}-{}-{}".format(date_str[:4], date_str[4:6], date_str[6:8])
        last_update = pm_last.strftime("%Y-%m-%dT%H:%M:%S") if pm_last else iso_date

        conn.execute(
            "INSERT INTO machine_current_state "
            "(machine_id, state, mode, program, tool_num, drill_dia, since, last_update, "
            "counter, work_order, work_order_side) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(machine_id) DO UPDATE SET "
            "state=excluded.state, mode=excluded.mode, program=excluded.program, "
            "tool_num=excluded.tool_num, drill_dia=excluded.drill_dia, "
            "since=excluded.since, last_update=excluded.last_update, "
            "counter=excluded.counter, work_order=excluded.work_order, "
            "work_order_side=excluded.work_order_side",
            (machine_id, state, "AUTO", None, None, None, since, last_update,
             None, last_wo, None),
        )

        # Insert state transitions from RUN intervals
        for start, end in run_intervals:
            if end is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO state_transitions "
                    "(machine_id, timestamp, from_state, to_state, program, tool_num, drill_dia) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (machine_id, start.strftime("%Y-%m-%dT%H:%M:%S"),
                     "RESET", "RUN", None, None, None),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO state_transitions "
                    "(machine_id, timestamp, from_state, to_state, program, tool_num, drill_dia) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (machine_id, end.strftime("%Y-%m-%dT%H:%M:%S"),
                     "RUN", "RESET", None, None, None),
                )

        # UPSERT laser_work_orders
        for rec in unique_wo:
            # Convert timestamps to ISO format
            try:
                start_dt = datetime.datetime.strptime(rec["start_time"], "%Y/%m/%d %H:%M:%S")
                start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
            except ValueError:
                start_iso = rec["start_time"]

            try:
                end_dt = datetime.datetime.strptime(rec["end_time"], "%Y/%m/%d %H:%M:%S")
                end_iso = end_dt.strftime("%Y-%m-%dT%H:%M:%S")
            except ValueError:
                end_iso = rec.get("end_time")

            try:
                duration = float(rec["duration_secs"])
            except (ValueError, TypeError):
                duration = None

            conn.execute(
                "INSERT INTO laser_work_orders "
                "(machine_id, start_time, end_time, duration_secs, station, "
                "work_order, lsr_file_path, hole_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(machine_id, start_time, station) DO UPDATE SET "
                "end_time=excluded.end_time, duration_secs=excluded.duration_secs, "
                "work_order=excluded.work_order, lsr_file_path=excluded.lsr_file_path, "
                "hole_count=excluded.hole_count",
                (machine_id, start_iso, end_iso, duration, rec["station"],
                 rec["work_order"], rec.get("lsr_file_path", ""), rec.get("hole_count", 0)),
            )

        conn.commit()

    logger.info(
        "[%s] Done: %d hours, state=%s, %d work orders",
        machine_id, len(hourly), state, len(unique_wo),
    )


# =============================================================================
# F. Run Cycle
# =============================================================================

def run_parser_cycle(db_path=None, settings=None, machines_config=None):
    """Execute one parse cycle for all enabled laser machines.

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

    laser_machines = get_machines_by_type(machines_config, "kataoka")
    if not laser_machines:
        logger.debug("No enabled laser machines found.")
        return

    today = datetime.date.today()
    date_str = today.strftime("%Y%m%d")
    backup_root = get_backup_root(settings)

    logger.info("Laser parser cycle: %d machines, date=%s", len(laser_machines), date_str)

    for machine in laser_machines:
        machine_id = machine["id"]
        log_dir = os.path.join(backup_root, machine_id, date_str)
        programs_dir = os.path.join(backup_root, machine_id, "programs")
        lsr_dir = os.path.join(backup_root, machine_id, "lsr_files")

        try:
            parse_laser_machine(db_path, machine_id, log_dir, programs_dir, lsr_dir, date_str)
        except Exception as e:
            logger.error("[%s] Laser parser error: %s", machine_id, e, exc_info=True)

    try:
        check_db_archive(settings)
    except Exception as e:
        logger.error("Archive check error: %s", e, exc_info=True)

    logger.info("Laser parser cycle complete.")


def run_parser_loop(interval=None):
    """Run laser parser in a continuous loop.

    Args:
        interval: Seconds between cycles. Reads from settings if None.
    """
    settings = load_settings()
    if interval is None:
        interval = settings.get("poll_interval_seconds", 600)

    logger.info("Starting laser parser loop (interval=%ds)", interval)

    while True:
        try:
            run_parser_cycle(settings=settings)
        except Exception as e:
            logger.error("Laser parser cycle failed: %s", e, exc_info=True)
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
        print("  python parsers/laser_log_parser.py --once   # Run one cycle")
        print("  python parsers/laser_log_parser.py --loop   # Run continuously")

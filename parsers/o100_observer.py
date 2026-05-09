"""O100.txt observer — polls each Takeuchi machine's NcProgram\\O100.txt and
records snapshots whenever content changes.

Two entry points:
    1. Background thread (run_observer_loop / start_observer_thread):
       polls every 300s, stat-then-read on mtime/size change.
    2. record_tx1_triggered_snapshot: called by tx1_log_parser when a
       FILEOPERATION LOAD event for O100.txt is detected — reads live SMB
       and records with trigger_source='tx1_event'. The TX1 timestamp is
       TZ-corrected per machines.json `tx1_tz_offset_hours` (M14/M15/M18=1).

Both paths feed the same `record_snapshot` helper, which is idempotent via
UNIQUE(machine_id, content_hash, smb_mtime).

Dev-env safe: SMB paths fail to resolve on non-Windows, observer logs
debug and stays idle. Production must run on the compute PC.

Reference: notes/mech_drill_board_identification.md Phase 3.
"""

import datetime
import json
import logging
import os
import platform
import threading
import time

from parsers.base_parser import (
    get_db_connection,
    get_db_path,
    get_machines_by_type,
    load_machines_config,
    load_settings,
)
from parsers.o100_parser import parse_o100_content

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 300  # 5 minutes — matches Phase 3 design
SMB_NCPROGRAM_TEMPLATE = r"\\{ip}\NcProgram\O100.txt"
# Takeuchi O100.txt is small (<2KB typical, 5KB worst case) and ASCII/CP932.
MAX_FILE_SIZE_BYTES = 64 * 1024


def _smb_path_for(ip):
    return SMB_NCPROGRAM_TEMPLATE.format(ip=ip)


def _stat_o100(ip):
    """Return (size, mtime_iso) for remote O100.txt, or (None, None) on error."""
    if platform.system() != "Windows":
        return None, None
    try:
        st = os.stat(_smb_path_for(ip))
        return st.st_size, datetime.datetime.fromtimestamp(st.st_mtime).isoformat()
    except (FileNotFoundError, OSError):
        return None, None


def _read_o100(ip):
    """Read O100.txt content as text. CP932 with permissive errors.

    Returns the decoded string, or None on failure.
    """
    if platform.system() != "Windows":
        return None
    path = _smb_path_for(ip)
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size > MAX_FILE_SIZE_BYTES:
        # Defensive: O100.txt is always tiny. Anything huge is an anomaly
        # we should not load into memory blindly.
        logger.warning("O100.txt at %s is %d bytes — skipping (max=%d)",
                       path, size, MAX_FILE_SIZE_BYTES)
        return None
    try:
        with open(path, "rb") as f:
            raw = f.read()
        return raw.decode("cp932", errors="replace")
    except OSError as e:
        logger.debug("Read failed %s: %s", path, e)
        return None


def record_snapshot(conn, machine_id, content, smb_size, smb_mtime_iso,
                    trigger_source, tx1_event_ts=None):
    """Parse content and insert into o100_snapshots + update machine_current_state.

    Idempotent via UNIQUE(machine_id, content_hash, smb_mtime). Returns the
    parsed dict (with active_subs / content_hash) so callers can update
    their per-machine cache.

    Args:
        conn: open SQLite connection.
        machine_id: e.g. 'M14'.
        content: full O100.txt text.
        smb_size: bytes, may be None.
        smb_mtime_iso: ISO timestamp string, may be None.
        trigger_source: 'mtime_change' | 'tx1_event' | 'initial'.
        tx1_event_ts: TZ-corrected ISO ts when trigger_source='tx1_event'.

    Returns:
        dict from parse_o100_content, or None if content was empty/unparseable.
    """
    if not content:
        return None
    parsed = parse_o100_content(content)
    captured_at = datetime.datetime.now().isoformat()
    active_subs_json = json.dumps(parsed["active_subs"])

    try:
        conn.execute(
            "INSERT OR IGNORE INTO o100_snapshots "
            "(machine_id, captured_at, trigger_source, smb_mtime, smb_size, "
            " content_hash, active_subs, raw_content, tx1_event_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (machine_id, captured_at, trigger_source, smb_mtime_iso, smb_size,
             parsed["content_hash"], active_subs_json, content, tx1_event_ts),
        )
    except Exception as e:
        logger.warning("[%s] o100 snapshot insert failed: %s", machine_id, e)
        return parsed

    # Always reflect the latest observed state on machine_current_state, even
    # if the snapshot row was IGNORE'd by UNIQUE — captured_at moves forward.
    try:
        conn.execute(
            "UPDATE machine_current_state SET "
            "current_o100_subs=?, current_o100_hash=?, o100_captured_at=? "
            "WHERE machine_id=?",
            (active_subs_json, parsed["content_hash"], captured_at, machine_id),
        )
    except Exception as e:
        logger.warning("[%s] machine_current_state o100 update failed: %s", machine_id, e)

    return parsed


def _read_and_record(conn, machine_id, ip, trigger_source, tx1_event_ts=None):
    """Stat + read + record. Used by both polling loop and TX1 hook.

    Returns (smb_size, smb_mtime_iso, parsed_or_none) so caller can update cache.
    """
    size, mtime_iso = _stat_o100(ip)
    if size is None:
        return None, None, None
    content = _read_o100(ip)
    if content is None:
        return size, mtime_iso, None
    parsed = record_snapshot(
        conn, machine_id, content, size, mtime_iso, trigger_source, tx1_event_ts,
    )
    return size, mtime_iso, parsed


def record_tx1_triggered_snapshot(conn, machine_id, ip, tx1_event_ts_raw,
                                  tz_offset_hours):
    """Called by tx1_log_parser when a LoadProgram O100.txt event is parsed.

    TX1 ts uses per-machine TZ (M14/M15/M18=JST=UTC+9, others=TST=UTC+8 per
    Phase 4 #1 finding); we shift to TST so it can join with snapshot
    captured_at (server time, TST).

    Failures are logged and swallowed — TX1 parser shouldn't break because
    the SMB share is unreachable.
    """
    try:
        event_dt = datetime.datetime.fromisoformat(tx1_event_ts_raw)
        if tz_offset_hours:
            event_dt = event_dt - datetime.timedelta(hours=tz_offset_hours)
        tx1_event_ts = event_dt.isoformat()
    except (ValueError, TypeError):
        tx1_event_ts = tx1_event_ts_raw

    try:
        _read_and_record(conn, machine_id, ip, "tx1_event", tx1_event_ts)
    except Exception as e:
        logger.warning("[%s] tx1-triggered o100 read failed: %s", machine_id, e)


def run_observer_loop(db_path=None, interval=DEFAULT_INTERVAL_SEC,
                       settings=None, machines_config=None, stop_event=None):
    """Main 5-min polling loop. Call from a daemon thread.

    For each takeuchi machine: stat NcProgram\\O100.txt; on mtime/size change
    (or first stat per process), read + record_snapshot.

    The observer keeps a per-machine cache of (size, mtime) so unchanged files
    cost only a single os.stat call (no read).
    """
    if settings is None:
        settings = load_settings()
    if machines_config is None:
        machines_config = load_machines_config()
    if db_path is None:
        db_path = get_db_path(settings)

    logger.info("o100_observer started (interval=%ds)", interval)

    # Per-machine cache: (size, mtime_iso). On first run we record an
    # 'initial' snapshot so the dashboard has data immediately.
    last_seen = {}

    while True:
        if stop_event is not None and stop_event.is_set():
            logger.info("o100_observer stopping")
            return

        cycle_start = time.time()
        try:
            takeuchi_machines = get_machines_by_type(machines_config, "takeuchi")
        except Exception as e:
            logger.error("o100_observer: list machines failed: %s", e)
            takeuchi_machines = []

        if takeuchi_machines:
            try:
                conn = get_db_connection(db_path)
            except Exception as e:
                logger.error("o100_observer: db connect failed: %s", e)
                conn = None
        else:
            conn = None

        try:
            for machine in takeuchi_machines:
                machine_id = machine["id"]
                ip = machine.get("ip")
                if not ip or conn is None:
                    continue

                size, mtime_iso = _stat_o100(ip)
                if mtime_iso is None:
                    continue  # unreachable / dev-env

                prev = last_seen.get(machine_id)
                first_seen = prev is None
                changed = first_seen or (prev[0] != size) or (prev[1] != mtime_iso)
                if not changed:
                    continue

                trigger = "initial" if first_seen else "mtime_change"
                _, _, parsed = _read_and_record(conn, machine_id, ip, trigger)
                last_seen[machine_id] = (size, mtime_iso)
                if parsed is not None:
                    logger.info(
                        "[%s] o100 %s: active_subs=%s hash=%s",
                        machine_id, trigger, parsed["active_subs"],
                        parsed["content_hash"][:12],
                    )

            if conn is not None:
                conn.commit()
        finally:
            if conn is not None:
                conn.close()

        elapsed = time.time() - cycle_start
        sleep_for = max(0.0, interval - elapsed)
        while sleep_for > 0:
            if stop_event is not None and stop_event.is_set():
                logger.info("o100_observer stopping")
                return
            step = min(1.0, sleep_for)
            time.sleep(step)
            sleep_for -= step


def start_observer_thread(db_path=None, interval=DEFAULT_INTERVAL_SEC,
                           settings=None, machines_config=None):
    """Start the observer in a daemon thread. Returns (thread, stop_event)."""
    stop_event = threading.Event()
    thread = threading.Thread(
        target=run_observer_loop,
        kwargs={
            "db_path": db_path,
            "interval": interval,
            "settings": settings,
            "machines_config": machines_config,
            "stop_event": stop_event,
        },
        daemon=True,
        name="o100-observer",
    )
    thread.start()
    return thread, stop_event

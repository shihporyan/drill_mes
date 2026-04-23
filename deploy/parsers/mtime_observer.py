"""High-frequency TX1.Log mtime observer (background thread).

Every `interval` seconds, `os.stat()` the remote TX1.Log on each enabled
Takeuchi machine. Only inserts a row into `tx1_mtime_events` when the
SMB-reported mtime has actually advanced since the last observation for
that machine. Each row therefore represents "a TX1 flush became visible
to the compute PC at this time."

Purpose: validate the hypothesis that state transitions trigger TX1
flushes (cross-join with `state_transitions` for transition→flush
latency). See notes/tx1_flush_latency_investigation.md Layer 1.

Cost: 18 machines × 30s = ~36 stat calls/min. Very light.

Dev-env safe: On non-Windows hosts SMB paths don't resolve and every
stat fails; the thread keeps running but writes nothing (silent).
"""

import datetime
import logging
import os
import platform
import threading
import time

from parsers.base_parser import (
    get_db_connection,
    load_settings,
    load_machines_config,
    get_machines_by_type,
    get_db_path,
)

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 30


def _stat_tx1(ip, day_prefix):
    """Return (size, mtime_iso) for remote TX1.Log, or (None, None) on error."""
    if platform.system() != "Windows":
        return None, None
    smb_path = "\\\\{}\\LOG\\{}TX1.Log".format(ip, day_prefix)
    try:
        st = os.stat(smb_path)
        return st.st_size, datetime.datetime.fromtimestamp(st.st_mtime).isoformat()
    except (FileNotFoundError, OSError):
        return None, None


def run_observer_loop(db_path=None, interval=DEFAULT_INTERVAL_SEC,
                       settings=None, machines_config=None, stop_event=None):
    """Main observer loop. Call from a daemon thread.

    Args:
        db_path: SQLite path. Defaults from settings.
        interval: Seconds between polls (default 30).
        settings: Optional settings dict.
        machines_config: Optional machines config dict.
        stop_event: Optional threading.Event to signal shutdown.
    """
    if settings is None:
        settings = load_settings()
    if machines_config is None:
        machines_config = load_machines_config()
    if db_path is None:
        db_path = get_db_path(settings)

    logger.info("mtime_observer started (interval=%ds)", interval)

    # Per-machine cache of last-seen (mtime, size) so we only write when
    # mtime advances. Starts empty; first stat per machine writes a baseline.
    last_seen = {}

    while True:
        if stop_event is not None and stop_event.is_set():
            logger.info("mtime_observer stopping")
            return

        cycle_start = time.time()

        # Reload machines each cycle so enable/disable changes take effect
        # without restarting the service.
        try:
            takeuchi_machines = get_machines_by_type(machines_config, "takeuchi")
        except Exception as e:
            logger.error("mtime_observer: failed to list machines: %s", e)
            takeuchi_machines = []

        today = datetime.date.today()
        day_prefix = today.strftime("%d")

        if takeuchi_machines:
            try:
                conn = get_db_connection(db_path)
            except Exception as e:
                logger.error("mtime_observer: db connect failed: %s", e)
                conn = None
        else:
            conn = None

        try:
            for machine in takeuchi_machines:
                machine_id = machine["id"]
                ip = machine.get("ip")
                if not ip:
                    continue

                size, mtime_iso = _stat_tx1(ip, day_prefix)
                if mtime_iso is None:
                    # Unreachable/dev-env; skip silently
                    continue

                prev = last_seen.get(machine_id)
                if prev is not None and prev[1] == mtime_iso:
                    # No change — most common path, do nothing
                    continue

                size_delta = (size - prev[0]) if prev is not None and prev[0] is not None and size is not None else None
                observed_at = datetime.datetime.now().isoformat()
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO tx1_mtime_events "
                        "(machine_id, observed_at, new_mtime, size_delta, new_size) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (machine_id, observed_at, mtime_iso, size_delta, size),
                    )
                except Exception as e:
                    logger.warning("mtime_observer [%s] insert failed: %s", machine_id, e)
                    continue

                last_seen[machine_id] = (size, mtime_iso)

            if conn is not None:
                conn.commit()
        finally:
            if conn is not None:
                conn.close()

        elapsed = time.time() - cycle_start
        sleep_for = max(0.0, interval - elapsed)
        # Break sleep into 1s chunks so stop_event is responsive
        while sleep_for > 0:
            if stop_event is not None and stop_event.is_set():
                logger.info("mtime_observer stopping")
                return
            step = min(1.0, sleep_for)
            time.sleep(step)
            sleep_for -= step


def start_observer_thread(db_path=None, interval=DEFAULT_INTERVAL_SEC,
                            settings=None, machines_config=None):
    """Start the observer in a daemon thread. Returns (thread, stop_event).

    The caller may set stop_event to request graceful shutdown (the thread
    stops at the next poll boundary, within `interval` seconds).
    """
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
        name="tx1-mtime-observer",
    )
    thread.start()
    return thread, stop_event

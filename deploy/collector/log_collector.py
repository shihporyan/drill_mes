"""
Log collector: copies Drive.Log files from machine SMB shares to local backup.

Uses robocopy (Windows) to copy log files from each enabled machine's
SMB share to the local backup directory.

On non-Windows systems (dev), logs a warning and skips robocopy.

Usage:
    python collector/log_collector.py          # Run once
    python collector/log_collector.py --loop   # Run continuously
"""

import datetime
import logging
import os
import platform
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from parsers.base_parser import (
    load_machines_config,
    load_settings,
    get_enabled_machines,
    get_backup_root,
    get_db_connection,
    get_db_path,
)

logger = logging.getLogger(__name__)


def should_skip_backoff(db_path, machine_id, settings):
    """Check if a machine should be skipped due to consecutive failures.

    After 3 consecutive failures (configurable via backoff_threshold),
    only retry every 30 minutes (configurable via backoff_interval_seconds)
    instead of every poll cycle.

    Args:
        db_path: Path to SQLite database.
        machine_id: Machine identifier.
        settings: Settings dict.

    Returns:
        bool: True if the machine should be skipped this cycle.
    """
    threshold = settings.get("backoff_threshold", 3)
    interval = settings.get("backoff_interval_seconds", 1800)

    try:
        with get_db_connection(db_path) as conn:
            cursor = conn.execute(
                "SELECT consecutive_fails, last_check FROM machine_health WHERE machine_id=?",
                (machine_id,),
            )
            row = cursor.fetchone()
            if not row:
                return False

            fails, last_check = row
            if fails < threshold:
                return False

            if last_check:
                last_dt = datetime.datetime.fromisoformat(last_check)
                elapsed = (datetime.datetime.now() - last_dt).total_seconds()
                if elapsed < interval:
                    logger.debug(
                        "[%s] Backoff: %d consecutive fails, next retry in %ds",
                        machine_id, fails, int(interval - elapsed),
                    )
                    return True

            return False
    except Exception:
        return False


def collect_logs_for_machine(machine, settings):
    """Run robocopy to copy Drive.Log from one machine's SMB share.

    Args:
        machine: Machine config dict with id, ip fields.
        settings: Settings dict with backup_root.

    Returns:
        bool: True if copy succeeded or no new files, False on error.
    """
    machine_id = machine["id"]
    ip = machine["ip"]
    share_name = "LOG"
    today = datetime.date.today()
    date_dir = today.strftime("%Y%m%d")

    source = "\\\\{}\\{}".format(ip, share_name)
    dest = os.path.join(get_backup_root(settings), machine_id, date_dir)

    if platform.system() != "Windows":
        logger.info("[%s] Skipping robocopy on %s (non-Windows dev environment)", machine_id, platform.system())
        return True

    # Ensure destination directory exists
    os.makedirs(dest, exist_ok=True)

    # robocopy with retry=1, wait=1sec, only *Drive.Log files
    cmd = [
        "robocopy", source, dest, "*Drive.Log",
        "/R:1", "/W:1",
    ]

    logger.info("[%s] Running: %s", machine_id, " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        # robocopy exit codes: 0=no change, 1=files copied, 2+=errors
        # Codes 0-7 are considered success
        if result.returncode <= 7:
            logger.info("[%s] robocopy OK (exit code %d)", machine_id, result.returncode)
            return True
        else:
            logger.warning("[%s] robocopy failed (exit code %d): %s",
                           machine_id, result.returncode, result.stderr[:200])
            return False
    except subprocess.TimeoutExpired:
        logger.error("[%s] robocopy timed out", machine_id)
        return False
    except FileNotFoundError:
        logger.error("[%s] robocopy not found. Is this Windows?", machine_id)
        return False
    except Exception as e:
        logger.error("[%s] robocopy error: %s", machine_id, e)
        return False


def update_machine_health(db_path, machine_id, success):
    """Update machine_health table after collection attempt.

    Args:
        db_path: Path to SQLite database.
        machine_id: Machine identifier.
        success: Whether the collection was successful.
    """
    now = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    with get_db_connection(db_path) as conn:
        if success:
            conn.execute(
                "INSERT INTO machine_health "
                "(machine_id, is_online, last_seen, offline_since, consecutive_fails, last_check) "
                "VALUES (?, 1, ?, NULL, 0, ?) "
                "ON CONFLICT(machine_id) DO UPDATE SET "
                "is_online=1, last_seen=excluded.last_seen, "
                "offline_since=NULL, consecutive_fails=0, last_check=excluded.last_check",
                (machine_id, now, now),
            )
        else:
            # Get current consecutive_fails
            cursor = conn.execute(
                "SELECT consecutive_fails, offline_since FROM machine_health WHERE machine_id=?",
                (machine_id,),
            )
            row = cursor.fetchone()
            fails = (row[0] + 1) if row else 1
            offline_since = row[1] if row and row[1] else now

            conn.execute(
                "INSERT INTO machine_health "
                "(machine_id, is_online, last_seen, offline_since, consecutive_fails, last_check) "
                "VALUES (?, 0, NULL, ?, ?, ?) "
                "ON CONFLICT(machine_id) DO UPDATE SET "
                "is_online=0, offline_since=COALESCE(machine_health.offline_since, excluded.offline_since), "
                "consecutive_fails=excluded.consecutive_fails, last_check=excluded.last_check",
                (machine_id, offline_since, fails, now),
            )
        conn.commit()


def run_collection_cycle(settings=None, machines_config=None, db_path=None):
    """Execute one collection cycle for all enabled machines.

    Args:
        settings: Optional settings override.
        machines_config: Optional machines config override.
        db_path: Optional database path override.
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

    logger.info("Collection cycle: %d enabled machines", len(enabled))

    for machine in enabled:
        if should_skip_backoff(db_path, machine["id"], settings):
            continue
        success = collect_logs_for_machine(machine, settings)
        try:
            update_machine_health(db_path, machine["id"], success)
        except Exception as e:
            logger.error("[%s] Failed to update health: %s", machine["id"], e)

    logger.info("Collection cycle complete.")


def run_collection_loop(interval=None):
    """Run collector in a continuous loop.

    Args:
        interval: Seconds between cycles. Reads from settings if None.
    """
    settings = load_settings()
    if interval is None:
        interval = settings.get("poll_interval_seconds", 600)

    logger.info("Starting collection loop (interval=%ds)", interval)

    while True:
        try:
            run_collection_cycle(settings=settings)
        except Exception as e:
            logger.error("Collection cycle failed: %s", e, exc_info=True)
        logger.info("Next collection in %d seconds...", interval)
        time.sleep(interval)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if len(sys.argv) > 1 and sys.argv[1] == "--loop":
        run_collection_loop()
    elif len(sys.argv) > 1 and sys.argv[1] == "--once":
        run_collection_cycle()
    else:
        print("Usage:")
        print("  python collector/log_collector.py --once   # Run one cycle")
        print("  python collector/log_collector.py --loop   # Run continuously")

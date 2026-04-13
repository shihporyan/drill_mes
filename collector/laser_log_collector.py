"""
Laser log collector: copies log files from Kataoka laser machine SMB shares.

Copies three types of files:
1. System logs (ClsLaserCom, PhysicalMemory, ClsPLCTrd) from Log/
2. Work order info (ProcTimeEnd, ProcTimeStart) from Info/
3. LSR drill coordinate files from Desktop (paths extracted from ProcTimeEnd)

On non-Windows systems (dev), logs a warning and skips robocopy.

Usage:
    python collector/laser_log_collector.py --once
    python collector/laser_log_collector.py --loop
"""

import csv
import datetime
import io
import logging
import os
import platform
import re
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from parsers.base_parser import (
    load_machines_config,
    load_settings,
    get_machines_by_type,
    get_backup_root,
    get_db_connection,
    get_db_path,
)
from collector.log_collector import update_machine_health, should_skip_backoff

logger = logging.getLogger(__name__)


def _robocopy(source, dest, file_pattern, machine_id, timeout=30):
    """Run robocopy to copy files from SMB share.

    Args:
        source: UNC source path.
        dest: Local destination path.
        file_pattern: File glob pattern (e.g. '*_20260407_*.log').
        machine_id: For logging.
        timeout: Seconds before timeout.

    Returns:
        bool: True if succeeded.
    """
    if platform.system() != "Windows":
        logger.info("[%s] Skipping robocopy on %s (non-Windows dev)", machine_id, platform.system())
        return True

    os.makedirs(dest, exist_ok=True)

    cmd = ["robocopy", source, dest, file_pattern, "/R:1", "/W:1"]
    logger.info("[%s] Running: %s", machine_id, " ".join(cmd))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode <= 7:
            logger.info("[%s] robocopy OK (exit code %d)", machine_id, result.returncode)
            return True
        else:
            logger.warning("[%s] robocopy failed (exit code %d)", machine_id, result.returncode)
            return False
    except subprocess.TimeoutExpired:
        logger.error("[%s] robocopy timed out", machine_id)
        return False
    except FileNotFoundError:
        logger.error("[%s] robocopy not found", machine_id)
        return False
    except Exception as e:
        logger.error("[%s] robocopy error: %s", machine_id, e)
        return False


def collect_system_logs(machine, machines_config, settings):
    """Collect system log files (ClsLaserCom, PhysicalMemory, etc.).

    Source: \\{ip}\{laser_log_path}\
    Pattern: *_{YYYYMMDD}_*.log
    Dest: backup_root/{machine_id}/{YYYYMMDD}/

    Args:
        machine: Machine config dict.
        machines_config: Full machines config.
        settings: Settings dict.

    Returns:
        bool: True if succeeded.
    """
    machine_id = machine["id"]
    ip = machine["ip"]
    today = datetime.date.today()
    date_str = today.strftime("%Y%m%d")

    laser_log_path = machines_config.get("laser_log_path", r"D$\LaserDrillingProcess\Log")
    source = "\\\\{}\\{}".format(ip, laser_log_path)
    dest = os.path.join(get_backup_root(settings), machine_id, date_str)

    # Copy today's log files. Production files are named like '20260408_ClsLaserCom.log'
    # (date is the prefix, no leading random number).
    pattern = "{}_*.log".format(date_str)
    return _robocopy(source, dest, pattern, machine_id)


def collect_program_info(machine, machines_config, settings):
    """Collect ProcTimeEnd and ProcTimeStart files.

    Source: \\{ip}\{laser_info_path}\
    Dest: backup_root/{machine_id}/programs/

    Args:
        machine: Machine config dict.
        machines_config: Full machines config.
        settings: Settings dict.

    Returns:
        bool: True if succeeded.
    """
    machine_id = machine["id"]
    ip = machine["ip"]
    today = datetime.date.today()

    laser_info_path = machines_config.get("laser_info_path", r"D$\LaserDrillingProcess\Info")
    source = "\\\\{}\\{}".format(ip, laser_info_path)
    dest = os.path.join(get_backup_root(settings), machine_id, "programs")

    # Copy current month's ProcTimeEnd. Production uses .log extension.
    month_prefix = today.strftime("%Y%m")
    ok1 = _robocopy(source, dest, "{}_ProcTimeEnd.log".format(month_prefix), machine_id)

    # Copy ProcTimeStart (current job, overwritten each time)
    ok2 = _robocopy(source, dest, "ProcTimeStart.log", machine_id)

    return ok1 and ok2


def collect_lsr_files(machine, machines_config, settings):
    """Collect LSR files referenced in ProcTimeEnd.

    Parses the local copy of ProcTimeEnd to find LSR file paths,
    then copies each LSR file from the machine's Desktop.

    Source: \\{ip}\{laser_desktop_path}\{wo_folder}\{lsr_file}
    Dest: backup_root/{machine_id}/lsr_files/

    Args:
        machine: Machine config dict.
        machines_config: Full machines config.
        settings: Settings dict.

    Returns:
        bool: True if succeeded.
    """
    machine_id = machine["id"]
    ip = machine["ip"]

    laser_desktop_path = machines_config.get("laser_desktop_path", r"C$\Users\KATAOKA\Desktop")
    programs_dir = os.path.join(get_backup_root(settings), machine_id, "programs")
    lsr_dest = os.path.join(get_backup_root(settings), machine_id, "lsr_files")

    if not os.path.isdir(programs_dir):
        return True  # No programs collected yet

    # Parse ProcTimeEnd to find LSR paths
    lsr_paths = set()
    for filename in os.listdir(programs_dir):
        if "ProcTimeEnd" not in filename and "ProcTimeStart" not in filename:
            continue

        filepath = os.path.join(programs_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        reader = csv.reader(io.StringIO(line))
                        fields = next(reader)
                    except Exception:
                        continue
                    # Detail lines have LSR path in 3rd field
                    for field in fields:
                        if field.lower().endswith(".lsr"):
                            lsr_paths.add(field)
        except Exception as e:
            logger.warning("[%s] Error reading %s: %s", machine_id, filename, e)

    if not lsr_paths:
        return True

    # Check which LSR files we don't have yet
    os.makedirs(lsr_dest, exist_ok=True)
    all_ok = True

    for lsr_path in lsr_paths:
        basename = os.path.basename(lsr_path)
        local_path = os.path.join(lsr_dest, basename)

        if os.path.exists(local_path):
            continue  # Already collected

        # Extract the folder from the full Windows path
        # e.g. C:\Users\KATAOKA\Desktop\WD-2604008\WD-2604008-TOP-A.lsr
        # We need the parent folder name to build the SMB path
        parts = lsr_path.replace("/", "\\").split("\\")
        # Find Desktop in the path and build from there
        try:
            desktop_idx = next(i for i, p in enumerate(parts) if p.lower() == "desktop")
            relative = "\\".join(parts[desktop_idx + 1:])
            # Only need the folder, not the file
            relative_folder = "\\".join(parts[desktop_idx + 1:-1])
        except StopIteration:
            logger.warning("[%s] Cannot parse LSR path: %s", machine_id, lsr_path)
            continue

        source = "\\\\{}\\{}\\{}".format(ip, laser_desktop_path, relative_folder)
        ok = _robocopy(source, lsr_dest, basename, machine_id)
        if not ok:
            all_ok = False

    return all_ok


def run_collection_cycle(settings=None, machines_config=None, db_path=None):
    """Execute one collection cycle for all enabled laser machines.

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

    laser_machines = get_machines_by_type(machines_config, "kataoka")
    if not laser_machines:
        logger.debug("No enabled laser machines found.")
        return

    logger.info("Laser collection cycle: %d machines", len(laser_machines))

    for machine in laser_machines:
        machine_id = machine["id"]

        if should_skip_backoff(db_path, machine_id, settings):
            continue

        success = True

        # 1. System logs
        if not collect_system_logs(machine, machines_config, settings):
            success = False

        # 2. Program info (ProcTimeEnd, ProcTimeStart)
        if not collect_program_info(machine, machines_config, settings):
            success = False

        # 3. LSR files (based on paths in ProcTimeEnd)
        if not collect_lsr_files(machine, machines_config, settings):
            success = False

        # Update health
        try:
            update_machine_health(db_path, machine_id, success)
        except Exception as e:
            logger.error("[%s] Failed to update health: %s", machine_id, e)

    logger.info("Laser collection cycle complete.")


def run_collection_loop(interval=None):
    """Run laser collector in a continuous loop."""
    settings = load_settings()
    if interval is None:
        interval = settings.get("poll_interval_seconds", 600)

    logger.info("Starting laser collection loop (interval=%ds)", interval)

    while True:
        try:
            run_collection_cycle(settings=settings)
        except Exception as e:
            logger.error("Laser collection cycle failed: %s", e, exc_info=True)
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
        print("  python collector/laser_log_collector.py --once   # Run one cycle")
        print("  python collector/laser_log_collector.py --loop   # Run continuously")

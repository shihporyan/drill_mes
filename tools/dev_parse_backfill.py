"""
Parse all historical Drive.Log files in dev_logs directory.

Unlike run_parser_cycle() which only parses today's log,
this script scans all date directories and parses every Drive.Log found.

Usage:
    DRILL_DEV_CONFIG=config/settings.dev.json python tools/dev_parse_backfill.py
    DRILL_DEV_CONFIG=config/settings.dev.json python tools/dev_parse_backfill.py --date 2026-04-05
"""

import argparse
import logging
import os
import re
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from db.init_db import init_database
from parsers.base_parser import (
    load_settings,
    load_machines_config,
    get_enabled_machines,
    get_machines_by_type,
    get_backup_root,
    get_db_path,
)
from parsers.drive_log_parser import parse_log_file
from parsers.tx1_log_parser import parse_tx1_file
from parsers.laser_log_parser import parse_laser_machine

logger = logging.getLogger("backfill")


def backfill(target_date=None):
    """Parse all (or specific date) Drive.Log files in dev_logs.

    Args:
        target_date: Optional YYYYMMDD string to filter. None = parse all.
    """
    settings = load_settings()
    machines_config = load_machines_config()
    db_path = get_db_path(settings)
    backup_root = get_backup_root(settings)

    # Ensure DB exists
    init_database(db_path)

    enabled = get_enabled_machines(machines_config)
    if not enabled:
        logger.warning("No enabled machines found.")
        return

    total_parsed = 0

    for machine in enabled:
        machine_id = machine["id"]
        machine_type = machine.get("type", "takeuchi")
        machine_dir = os.path.join(backup_root, machine_id)

        if not os.path.isdir(machine_dir):
            logger.info("[%s] No directory found at %s, skipping.", machine_id, machine_dir)
            continue

        if machine_type == "kataoka":
            # Laser machine: use laser parser
            total_parsed += _backfill_laser(db_path, machine_id, machine_dir, target_date)
        else:
            # Takeuchi mechanical drill: use Drive.Log parser
            total_parsed += _backfill_takeuchi(db_path, machine_id, machine_dir, target_date)

    logger.info("Backfill complete: %d files/dates parsed.", total_parsed)


def _backfill_takeuchi(db_path, machine_id, machine_dir, target_date):
    """Backfill Takeuchi mechanical drill logs."""
    parsed = 0

    date_dirs = sorted(d for d in os.listdir(machine_dir)
                       if re.match(r"^\d{8}$", d) and os.path.isdir(os.path.join(machine_dir, d)))

    if target_date:
        date_dirs = [d for d in date_dirs if d == target_date]

    for date_dir in date_dirs:
        date_path = os.path.join(machine_dir, date_dir)

        for filename in sorted(os.listdir(date_path)):
            if not filename.endswith("Drive.Log"):
                continue

            day_prefix = filename.replace("Drive.Log", "")
            if not re.match(r"^\d{2}$", day_prefix):
                continue

            log_path = os.path.join(date_path, filename)
            logger.info("[%s] Parsing %s/%s", machine_id, date_dir, filename)

            try:
                parse_log_file(db_path, machine_id, log_path, day_prefix)
                parsed += 1
            except Exception as e:
                logger.error("[%s] Error parsing %s: %s", machine_id, filename, e, exc_info=True)

            # Also parse TX1.Log for work order tracking
            tx1_filename = "{}TX1.Log".format(day_prefix)
            tx1_path = os.path.join(date_path, tx1_filename)
            if os.path.exists(tx1_path):
                try:
                    logger.info("[%s] Parsing %s/%s", machine_id, date_dir, tx1_filename)
                    parse_tx1_file(db_path, machine_id, tx1_path, day_prefix)
                except Exception as e:
                    logger.error("[%s] Error parsing %s: %s", machine_id, tx1_filename, e, exc_info=True)

    return parsed


def _backfill_laser(db_path, machine_id, machine_dir, target_date):
    """Backfill Kataoka laser drill logs."""
    parsed = 0
    programs_dir = os.path.join(machine_dir, "programs")
    lsr_dir = os.path.join(machine_dir, "lsr_files")

    # Scan date directories (YYYYMMDD format)
    date_dirs = sorted(d for d in os.listdir(machine_dir)
                       if re.match(r"^\d{8}$", d) and os.path.isdir(os.path.join(machine_dir, d)))

    if target_date:
        date_dirs = [d for d in date_dirs if d == target_date]

    for date_dir in date_dirs:
        log_dir = os.path.join(machine_dir, date_dir)
        logger.info("[%s] Parsing laser logs for %s", machine_id, date_dir)

        try:
            parse_laser_machine(db_path, machine_id, log_dir, programs_dir, lsr_dir, date_dir)
            parsed += 1
        except Exception as e:
            logger.error("[%s] Error parsing laser logs for %s: %s", machine_id, date_dir, e, exc_info=True)

    return parsed


def main():
    parser = argparse.ArgumentParser(description="Backfill historical Drive.Log files")
    parser.add_argument("--date", help="Target date in YYYYMMDD format (e.g. 20260405). Default: all dates.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    target_date = args.date.replace("-", "") if args.date else None
    backfill(target_date)


if __name__ == "__main__":
    main()

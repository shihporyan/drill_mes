"""
Backfill hourly_utilization rows wiped by the Drive.Log replay bug.

The bug (fixed in commit 175954a): when a peek-ahead replay triggered a
full re-parse of NNDrive.Log and that file had cross-midnight rows from
the previous day at the top, the DELETE step wiped the previous day's
hourly_utilization for all dates in parsed_rows, and only a few seconds
of cross-midnight data survived via UPSERT +=. The signature is a date
where only hour=23 exists, with total_seconds in the 10s-1000s range.

This script takes a list of (machine_id, date) pairs, wipes the bogus
rows, resets parse_progress for the matching day_prefix, and re-parses
the canonical {DD}Drive.Log from the backup root.

Safety:
- MUST be run with DrillMonitor stopped
  (`taskkill /F /FI "WINDOWTITLE eq DrillMonitor"`)
  otherwise the running parser will race this script.
- Default mode is dry-run. Pass --apply to actually write.
- Makes a DB backup copy before any writes.

Usage:
    python tools/backfill_wiped_dates.py --dry-run     (default, no writes)
    python tools/backfill_wiped_dates.py --apply       (perform the backfill)
    python tools/backfill_wiped_dates.py --apply --targets M02:2026-04-22,M03:2026-04-22
"""

import argparse
import datetime
import logging
import os
import shutil
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from parsers.base_parser import load_settings, get_backup_root, get_db_path
from parsers.drive_log_parser import parse_log_file

logger = logging.getLogger("backfill_wiped")


# Production 2026-04-23 replay damage — verified from drill_monitor.db snapshot.
DEFAULT_TARGETS = [
    ("M02", "2026-04-22"),
    ("M03", "2026-04-22"),
    ("M04", "2026-04-22"),
    ("M05", "2026-04-22"),
]


def resolve_log_path(backup_root, machine_id, date_str):
    """Find the NNDrive.Log file for (machine_id, date_str).

    Looks in the same-day backup dir first, then today's backup dir
    (robocopy also copies yesterday's file into today's dir).
    """
    dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    day_prefix = dt.strftime("%d")
    log_name = "{}Drive.Log".format(day_prefix)

    candidates = [
        os.path.join(backup_root, machine_id, dt.strftime("%Y%m%d"), log_name),
    ]
    today_dir = datetime.date.today().strftime("%Y%m%d")
    if today_dir != dt.strftime("%Y%m%d"):
        candidates.append(
            os.path.join(backup_root, machine_id, today_dir, log_name)
        )

    for p in candidates:
        if os.path.exists(p):
            return p, day_prefix
    return None, day_prefix


def snapshot_row(conn, machine_id, date_str):
    """Return (hour_count, sum_total_seconds, sum_hole_count, transition_count)."""
    h = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(total_seconds), 0), "
        "COALESCE(SUM(hole_count), 0) "
        "FROM hourly_utilization WHERE machine_id=? AND date=?",
        (machine_id, date_str),
    ).fetchone()
    t = conn.execute(
        "SELECT COUNT(*) FROM state_transitions "
        "WHERE machine_id=? AND timestamp LIKE ?",
        (machine_id, date_str + "%"),
    ).fetchone()
    return (h[0], h[1], h[2], t[0])


def backfill_one(db_path, backup_root, machine_id, date_str, apply):
    log_path, day_prefix = resolve_log_path(backup_root, machine_id, date_str)
    if log_path is None:
        logger.error("[%s %s] Drive.Log file not found under %s/%s/*/",
                     machine_id, date_str, backup_root, machine_id)
        return False

    logger.info("[%s %s] Source log: %s", machine_id, date_str, log_path)

    with sqlite3.connect(db_path) as conn:
        before = snapshot_row(conn, machine_id, date_str)
        logger.info(
            "[%s %s] BEFORE: %d hourly rows, total=%ds, holes=%d, transitions=%d",
            machine_id, date_str, *before,
        )

        if not apply:
            logger.info("[%s %s] dry-run: would DELETE and re-parse", machine_id, date_str)
            return True

        conn.execute(
            "DELETE FROM hourly_utilization WHERE machine_id=? AND date=?",
            (machine_id, date_str),
        )
        conn.execute(
            "DELETE FROM state_transitions WHERE machine_id=? AND timestamp LIKE ?",
            (machine_id, date_str + "%"),
        )
        conn.execute(
            "DELETE FROM parse_progress WHERE machine_id=? AND day_prefix=?",
            (machine_id, day_prefix),
        )
        conn.commit()

    # parse_log_file manages its own connection + commit
    parse_log_file(db_path, machine_id, log_path, day_prefix)

    with sqlite3.connect(db_path) as conn:
        after = snapshot_row(conn, machine_id, date_str)
    logger.info(
        "[%s %s] AFTER:  %d hourly rows, total=%ds, holes=%d, transitions=%d",
        machine_id, date_str, *after,
    )
    return True


def parse_targets(arg):
    pairs = []
    for tok in arg.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if ":" not in tok:
            raise ValueError("Bad target %r, expected MXX:YYYY-MM-DD" % tok)
        machine_id, date_str = tok.split(":", 1)
        pairs.append((machine_id.strip(), date_str.strip()))
    return pairs


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="Actually perform the backfill (default: dry-run).")
    ap.add_argument("--targets",
                    help="Comma-separated 'MXX:YYYY-MM-DD' pairs. "
                         "Default: the 4 rows damaged by the 2026-04-23 replay bug.")
    ap.add_argument("--db", help="Override DB path (default: from settings).")
    ap.add_argument("--backup-root",
                    help="Override backup_root (default: from settings).")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    settings = load_settings()
    db_path = args.db or get_db_path(settings)
    backup_root = args.backup_root or get_backup_root(settings)
    targets = parse_targets(args.targets) if args.targets else DEFAULT_TARGETS

    logger.info("DB:          %s", db_path)
    logger.info("Backup root: %s", backup_root)
    logger.info("Mode:        %s", "APPLY" if args.apply else "DRY-RUN")
    logger.info("Targets:     %s", targets)

    if args.apply:
        backup = db_path + ".bak_before_replay_backfill"
        if os.path.exists(backup):
            logger.error("Backup file already exists: %s — refuse to overwrite. "
                         "Move or delete it, then retry.", backup)
            sys.exit(1)
        shutil.copy2(db_path, backup)
        logger.info("DB backup:   %s", backup)

    ok = fail = 0
    for machine_id, date_str in targets:
        if backfill_one(db_path, backup_root, machine_id, date_str, args.apply):
            ok += 1
        else:
            fail += 1

    logger.info("Done. ok=%d fail=%d", ok, fail)
    sys.exit(0 if fail == 0 else 1)


if __name__ == "__main__":
    main()

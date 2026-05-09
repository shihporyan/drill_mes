"""Backfill laser hole_count for 5/2-5/8 (post-1d91a79 deploy gap).

5/9 health-check (notes/health_check_20260509.md C1+session-B finding) found
that laser_work_orders.hole_count is 0 for May rows even though raw ClsPLCTrd
has thousands of beam-OK events. Root cause: parser cycle parses today only,
so the 1d91a79 (4/30) ClsPLCTrd-based logic never re-runs against earlier
days. Production retention is 7 days, so we can only recover 5/2-5/8.

What this script does: for each (laser, date) in [5/2..5/8], call
parse_laser_machine() directly. UPSERT semantics in laser_log_parser will
overwrite existing 0 hole_count with the correct count. machine_current_state
is also touched on each iteration; we run dates ASCENDING so the final
iteration is closest to today (the next live cycle in <5 min will overwrite
it anyway). Today (5/9) is excluded — daily parser handles it live.

Usage:
    python tools/backfill_laser_holes_20260509.py            # dry run, show plan
    python tools/backfill_laser_holes_20260509.py --execute  # apply

Honors DRILL_DEV_CONFIG so dev-side smoke tests work:

    DRILL_DEV_CONFIG=config/settings.dev.json \
        python3 tools/backfill_laser_holes_20260509.py [--execute]
"""

import argparse
import datetime
import logging
import os
import re
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from parsers.base_parser import (
    load_settings, get_db_path, get_backup_root,
    load_machines_config, get_machines_by_type,
)
from parsers.laser_log_parser import parse_laser_machine

# Range to backfill. 5/9 excluded — daily cycle handles today.
BACKFILL_DATES = [
    "20260502", "20260503", "20260504", "20260505",
    "20260506", "20260507", "20260508",
]

BEAM_PATTERN = re.compile(
    r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}).*本加工データ取得.*加工基盤番号:(\d+)"
)


def quick_beam_count(filepath):
    """Stream-count beam-OK events in a ClsPLCTrd file. Returns int or -1 on error."""
    n = 0
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if BEAM_PATTERN.match(line):
                    n += 1
    except OSError:
        return -1
    return n


def report_plan(backup_root, machines):
    """Show which (machine, date) pairs have raw + how many beams each has."""
    print("=== Backfill plan ===")
    print(f'{"machine":4s}  {"date":10s}  {"file":40s}  {"size_KB":>10s}  {"beams":>8s}')
    plan = []
    for m in machines:
        if m.get("type") != "kataoka":
            continue
        mid = m["id"]
        for date_str in BACKFILL_DATES:
            iso = "{}-{}-{}".format(date_str[:4], date_str[4:6], date_str[6:])
            log_dir = os.path.join(backup_root, mid, date_str)
            plc = os.path.join(log_dir, "{}_ClsPLCTrd.log".format(date_str))
            if not os.path.isfile(plc):
                print(f'{mid:4s}  {iso:10s}  {os.path.basename(plc):40s}  {"-":>10s}  {"MISSING":>8s}')
                continue
            size = os.path.getsize(plc)
            n = quick_beam_count(plc)
            print(f'{mid:4s}  {iso:10s}  {os.path.basename(plc):40s}  {size//1024:>10}  {n:>8}')
            plan.append((mid, date_str, n))
    return plan


def db_state_before(conn):
    """Snapshot key counts before backfill so we can show a diff."""
    c = conn.cursor()
    nz_wo = c.execute("SELECT COUNT(*) FROM laser_work_orders WHERE hole_count > 0").fetchone()[0]
    sum_wo = c.execute("SELECT COALESCE(SUM(hole_count),0) FROM laser_work_orders").fetchone()[0]
    sum_h = c.execute(
        "SELECT COALESCE(SUM(hole_count),0) FROM hourly_utilization "
        "WHERE machine_id LIKE 'L%' AND date BETWEEN '2026-05-02' AND '2026-05-08'"
    ).fetchone()[0]
    return {"wo_nonzero": nz_wo, "wo_sum_holes": sum_wo, "hourly_sum_holes": sum_h}


def run_backfill(db_path, backup_root, machines, plan):
    """Iterate plan ascending date; call parse_laser_machine per (machine, date)."""
    print()
    print("=== Executing backfill ===")
    n_ok = 0
    n_err = 0
    # Sort ascending date so machine_current_state ends with the most recent
    plan_sorted = sorted(plan, key=lambda x: (x[1], x[0]))
    for mid, date_str, n_beams in plan_sorted:
        log_dir = os.path.join(backup_root, mid, date_str)
        programs_dir = os.path.join(backup_root, mid, "programs")
        try:
            parse_laser_machine(
                db_path, mid, log_dir, programs_dir, date_str,
                backup_root=backup_root,
            )
            print(f'  {mid} {date_str}: OK ({n_beams} beam events processed)')
            n_ok += 1
        except Exception as e:
            print(f'  {mid} {date_str}: ERROR {type(e).__name__}: {e}')
            n_err += 1
    return n_ok, n_err


def main():
    p = argparse.ArgumentParser(description="Backfill laser hole_count 5/2-5/8")
    p.add_argument("--execute", action="store_true", help="Apply changes (default: dry run)")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose parser logs")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    settings = load_settings()
    db_path = get_db_path(settings)
    backup_root = get_backup_root(settings)
    machines = [m for m in load_machines_config()["machines"] if m.get("enabled")]
    print(f'DB: {db_path}')
    print(f'backup_root: {backup_root}')
    print(f'Mode: {"EXECUTE" if args.execute else "DRY RUN"}')
    print()

    plan = report_plan(backup_root, machines)

    if not plan:
        print("No backfill candidates found (no raw ClsPLCTrd files).")
        return

    if not args.execute:
        print()
        print(f'Dry run — {len(plan)} (machine, date) pairs would be re-parsed.')
        print('Re-run with --execute to apply.')
        return

    conn = sqlite3.connect(db_path)
    before = db_state_before(conn)
    conn.close()

    n_ok, n_err = run_backfill(db_path, backup_root, machines, plan)

    conn = sqlite3.connect(db_path)
    after = db_state_before(conn)
    conn.close()

    print()
    print(f'=== Result: {n_ok} OK, {n_err} errors ===')
    print(f'  laser_work_orders nonzero rows:  {before["wo_nonzero"]:>6} -> {after["wo_nonzero"]:>6}')
    print(f'  laser_work_orders sum hole_count: {before["wo_sum_holes"]:>10,} -> {after["wo_sum_holes"]:>10,}')
    print(f'  hourly_utilization 5/2-5/8 sum:   {before["hourly_sum_holes"]:>10,} -> {after["hourly_sum_holes"]:>10,}')


if __name__ == "__main__":
    main()

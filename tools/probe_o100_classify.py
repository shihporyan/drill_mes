"""Classify each Takeuchi machine as M14-style (FILE.Log dumps O100.txt content)
or M13-style (FILE.Log only records Copy events).

This determines which extraction path Phase 3 needs to wire up per machine.

Run on production after at least one normal work day (so today's FILE.Log
has had chance to accumulate events). Reads from backup_root — no SMB needed.

Output:
    - stdout summary table per machine
    - tools/probe_results/o100_classify_{date}.csv

Usage:
    python tools/probe_o100_classify.py              # today only
    python tools/probe_o100_classify.py --days 7     # last 7 days
"""

import argparse
import csv
import datetime
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from parsers.base_parser import (
    load_machines_config,
    load_settings,
    get_enabled_machines,
    get_backup_root,
)


# An M14-style dump terminates each O100.txt LoadProgram block with [EOF].
# Counting [EOF] markers is a reliable signature distinct from M13's tiny
# Copy-event-only FILE.Log.
EOF_MARKER = "[EOF]"
LOAD_PREFIX = "LoadProgram"
DUMP_SIZE_THRESHOLD_BYTES = 4096   # M13 typical: ~150B; M14 typical: MB-scale


def scan_file_log(file_log_path):
    """Return (size_bytes, eof_count, load_o100_count) for one FILE.Log."""
    if not os.path.isfile(file_log_path):
        return None
    size = os.path.getsize(file_log_path)
    if size == 0:
        return (0, 0, 0)

    eof_count = 0
    load_o100_count = 0
    with open(file_log_path, "rb") as f:
        raw = f.read()
    text = raw.decode("cp932", errors="replace")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == EOF_MARKER:
            eof_count += 1
        elif LOAD_PREFIX in line and "O100.txt" in line:
            load_o100_count += 1
    return (size, eof_count, load_o100_count)


def classify(eof_count, load_o100_count, size):
    """Return one of: 'dump_style' | 'tx1_only_style' | 'inconclusive'."""
    if size is None:
        return "no_data"
    if eof_count > 0 and load_o100_count > 0:
        return "dump_style"
    if size < DUMP_SIZE_THRESHOLD_BYTES and eof_count == 0:
        return "tx1_only_style"
    return "inconclusive"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1,
                    help="Days back to scan (default: 1, today only)")
    args = ap.parse_args()

    settings = load_settings()
    backup_root = get_backup_root(settings)
    machines_cfg = load_machines_config()
    machines = [m for m in get_enabled_machines(machines_cfg)
                if m.get("type") == "takeuchi"]

    today = datetime.date.today()
    date_range = [today - datetime.timedelta(days=i) for i in range(args.days)]

    out_dir = os.path.join(PROJECT_ROOT, "tools", "probe_results")
    os.makedirs(out_dir, exist_ok=True)
    out_csv = os.path.join(out_dir, "o100_classify_{}.csv".format(today.strftime("%Y%m%d")))

    print("backup_root: {}".format(backup_root))
    print("scanning {} day(s) back to {}".format(args.days, date_range[-1]))
    print()

    rows = []
    summary = {}  # machine_id -> classification (worst across days)

    for m in machines:
        mid = m["id"]
        per_day = []
        for d in date_range:
            date_dir = d.strftime("%Y%m%d")
            dd = d.strftime("%d")
            file_log_path = os.path.join(backup_root, mid, date_dir,
                                         "{}FILE.Log".format(dd))
            result = scan_file_log(file_log_path)
            if result is None:
                cls = "no_data"
                size, eof, loads = 0, 0, 0
            else:
                size, eof, loads = result
                cls = classify(eof, loads, size)
            per_day.append((date_dir, size, eof, loads, cls))
            rows.append({
                "machine": mid,
                "date": date_dir,
                "file_log_size": size,
                "eof_markers": eof,
                "load_o100_count": loads,
                "classification": cls,
            })

        # Worst-case per-machine classification (any day with dump_style wins)
        any_dump = any(p[4] == "dump_style" for p in per_day)
        any_data = any(p[1] > 0 for p in per_day)
        if any_dump:
            summary[mid] = "dump_style"
        elif any_data:
            summary[mid] = "tx1_only_style"
        else:
            summary[mid] = "no_data"

    # Print table
    print("{:<5}  {:<16}  {:>10}  {:>5}  {:>5}".format(
        "M", "classification", "size_total", "EOFs", "LOADs"))
    print("-" * 50)
    for m in machines:
        mid = m["id"]
        my_rows = [r for r in rows if r["machine"] == mid]
        total_size = sum(r["file_log_size"] for r in my_rows)
        total_eof = sum(r["eof_markers"] for r in my_rows)
        total_loads = sum(r["load_o100_count"] for r in my_rows)
        print("{:<5}  {:<16}  {:>10}  {:>5}  {:>5}".format(
            mid, summary[mid], total_size, total_eof, total_loads))

    # Summary counts
    print()
    counts = {}
    for cls in summary.values():
        counts[cls] = counts.get(cls, 0) + 1
    for cls, n in sorted(counts.items()):
        print("  {}: {} machine(s)".format(cls, n))

    # Save CSV
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["machine", "date", "file_log_size",
                                          "eof_markers", "load_o100_count",
                                          "classification"])
        w.writeheader()
        w.writerows(rows)
    print()
    print("CSV: {}".format(out_csv))


if __name__ == "__main__":
    main()

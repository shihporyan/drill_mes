"""5/9 health-check cleanup — surgical fixes for two specific issues.

Findings from notes/health_check_20260509.md:

1. Four single-hour phantom hole spikes (5/5-5/7), all with run_seconds≈0
   except M10. UPDATE to zero hole_count (preserve run_seconds for util):
       M02 2026-05-06 h10: 175,413,429 holes
       M05 2026-05-07 h06: 169,019,025 holes
       M08 2026-05-07 h07: 178,094,240 holes
       M10 2026-05-05 h18: 154,922,175 holes (run_seconds=1178, keep)

2. Five April cross-midnight stub rows (hour=23 has 1-499s with all
   other hours empty — leftover from the pre-4/23 secondary-fix bug).
   These rows are pure artifacts; DELETE outright:
       M03 2026-04-03 h23: 375s
       M05 2026-04-20 h23: 348s
       M08 2026-04-01 h23: 384s
       M08 2026-04-03 h23: 499s
       M13 2026-04-20 h23:   1s

Run on the production compute PC (stdlib only — no sqlite3 CLI):

    python tools/cleanup_phantom_holes_20260509.py            # dry run
    python tools/cleanup_phantom_holes_20260509.py --execute  # apply

Honors DRILL_DEV_CONFIG so dev-side smoke tests work:

    DRILL_DEV_CONFIG=config/settings.dev.json \
        python3 tools/cleanup_phantom_holes_20260509.py [--execute]
"""

import argparse
import os
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from parsers.base_parser import load_settings, get_db_path


PHANTOM_HOLES = [
    # (machine_id, date, hour, expected_holes)
    ("M02", "2026-05-06", 10, 175413429),
    ("M05", "2026-05-07",  6, 169019025),
    ("M08", "2026-05-07",  7, 178094240),
    ("M10", "2026-05-05", 18, 154922175),
]

APRIL_STUBS = [
    # (machine_id, date, hour) — pure cross-midnight artifacts
    ("M03", "2026-04-03", 23),
    ("M05", "2026-04-20", 23),
    ("M08", "2026-04-01", 23),
    ("M08", "2026-04-03", 23),
    ("M13", "2026-04-20", 23),
]


def report_phantom(cur):
    print("=== Phantom hole spikes (UPDATE hole_count=0, keep run_seconds) ===")
    found = 0
    for mid, d, h, expected in PHANTOM_HOLES:
        row = cur.execute(
            "SELECT hole_count, run_seconds, total_seconds FROM hourly_utilization "
            "WHERE machine_id=? AND date=? AND hour=?",
            (mid, d, h),
        ).fetchone()
        if not row:
            print("  {} {} h{:02d}: NOT FOUND (already cleaned?)".format(mid, d, h))
            continue
        hc, rs, ts = row
        match = "✓" if hc == expected else "≠"
        print("  {} {} h{:02d}: hole_count={:,} (expected {:,} {}) "
              "run_s={} total_s={}".format(mid, d, h, hc, expected, match, rs, ts))
        found += 1
    return found


def report_stubs(cur):
    print("=== April cross-midnight stubs (DELETE row) ===")
    found = 0
    for mid, d, h in APRIL_STUBS:
        row = cur.execute(
            "SELECT total_seconds, run_seconds, hole_count FROM hourly_utilization "
            "WHERE machine_id=? AND date=? AND hour=?",
            (mid, d, h),
        ).fetchone()
        if not row:
            print("  {} {} h{:02d}: NOT FOUND".format(mid, d, h))
            continue
        ts, rs, hc = row
        # Confirm signature: should be hour=23 with low total_seconds and 0 holes
        # AND no other hour active that day
        same_day_active = cur.execute(
            "SELECT COUNT(*) FROM hourly_utilization "
            "WHERE machine_id=? AND date=? AND hour < 23 AND total_seconds > 0",
            (mid, d),
        ).fetchone()[0]
        sig_ok = "✓" if (ts < 1000 and hc == 0 and same_day_active == 0) else "≠"
        print("  {} {} h{:02d}: total_s={} run_s={} holes={} sig={}".format(
            mid, d, h, ts, rs, hc, sig_ok))
        found += 1
    return found


def apply_phantom(cur):
    n = 0
    for mid, d, h, _ in PHANTOM_HOLES:
        cur.execute(
            "UPDATE hourly_utilization SET hole_count=0 "
            "WHERE machine_id=? AND date=? AND hour=? AND hole_count > 50000000",
            (mid, d, h),
        )
        n += cur.rowcount
    return n


def apply_stubs(cur):
    n = 0
    for mid, d, h in APRIL_STUBS:
        # Belt-and-suspenders: only delete if it still matches the stub signature
        # (hour=23, total < 1000s, no other active hour same day).
        cur.execute(
            "DELETE FROM hourly_utilization "
            "WHERE machine_id=? AND date=? AND hour=? AND total_seconds < 1000 "
            "AND hole_count = 0 "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM hourly_utilization h2 "
            "  WHERE h2.machine_id=hourly_utilization.machine_id "
            "  AND h2.date=hourly_utilization.date "
            "  AND h2.hour < 23 AND h2.total_seconds > 0"
            ")",
            (mid, d, h),
        )
        n += cur.rowcount
    return n


def main():
    p = argparse.ArgumentParser(description="5/9 health-check phantom-hole cleanup")
    p.add_argument("--execute", action="store_true", help="Apply changes (default: dry run)")
    args = p.parse_args()

    settings = load_settings()
    db_path = get_db_path(settings)
    if not os.path.exists(db_path):
        print("ERROR: DB not found at {}".format(db_path))
        sys.exit(1)
    print("DB: {}".format(db_path))
    print("Mode: {}".format("EXECUTE" if args.execute else "DRY RUN"))
    print()

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        n_ph = report_phantom(cur)
        print()
        n_st = report_stubs(cur)
        print()

        if not args.execute:
            print("Dry run only. Re-run with --execute to apply.")
            return

        ph_done = apply_phantom(cur)
        st_done = apply_stubs(cur)
        conn.commit()
        print("Applied:")
        print("  phantom spikes zeroed: {} / {}".format(ph_done, n_ph))
        print("  April stubs deleted:   {} / {}".format(st_done, n_st))
        print()
        print("Verify:")
        rem = cur.execute(
            "SELECT COUNT(*) FROM hourly_utilization WHERE hole_count > 50000000"
        ).fetchone()[0]
        print("  hole_count > 50M remaining: {}".format(rem))
    finally:
        conn.close()


if __name__ == "__main__":
    main()

"""One-shot DB residue cleanup.

Removes:
- Pre-cutover March residue (sparse 2026-03-25..27 from initial laser
  testing — pre-2026-04-22 cutover).
- Two known backfill peek-ahead outliers
  (M03 / 2026-04-22 / 15:00 = 347M holes; M07 / 2026-04-21 / 20:00 = 197M).
- Defensive sweep: any row with hole_count > 50,000,000 (single machine
  cannot drill > 50M holes/hour; physical limit ~50K/hour).

The dashboard already filters these via DATA_START_DATE in api_server.py,
so this script is optional for visual cleanup. Running it shrinks the DB
and stops them surfacing in ad-hoc SQL queries.

Run on the production compute PC (no sqlite3 CLI required — uses stdlib):

    python tools\\cleanup_db_residue.py            # dry run, count only
    python tools\\cleanup_db_residue.py --execute  # actually delete + VACUUM
"""

import os
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from parsers.base_parser import load_settings, get_db_path

# (label, SQL). Counts are reported for both dry-run and execute.
DELETES = [
    ("Pre-cutover March residue (hourly_utilization)",
     "DELETE FROM hourly_utilization WHERE date < '2026-04-01'"),
    ("Pre-cutover March residue (state_transitions)",
     "DELETE FROM state_transitions WHERE timestamp < '2026-04-01'"),
    ("M03 outlier 2026-04-22 hour 15 (347M holes)",
     "DELETE FROM hourly_utilization "
     "WHERE machine_id = 'M03' AND date = '2026-04-22' AND hour = 15"),
    ("M07 outlier 2026-04-21 hour 20 (197M holes)",
     "DELETE FROM hourly_utilization "
     "WHERE machine_id = 'M07' AND date = '2026-04-21' AND hour = 20"),
    ("Defensive sweep (hole_count > 50,000,000)",
     "DELETE FROM hourly_utilization WHERE hole_count > 50000000"),
]


def snapshot_stats(cur):
    """Return a small dict of current counts/limits for before/after diff."""
    n_h = cur.execute("SELECT COUNT(*) FROM hourly_utilization").fetchone()[0]
    mn_h, mx_h = cur.execute(
        "SELECT MIN(date), MAX(date) FROM hourly_utilization"
    ).fetchone()
    max_holes = cur.execute(
        "SELECT MAX(hole_count) FROM hourly_utilization"
    ).fetchone()[0]
    n_t = cur.execute("SELECT COUNT(*) FROM state_transitions").fetchone()[0]
    return {
        "hourly_rows": n_h,
        "hourly_min_date": mn_h,
        "hourly_max_date": mx_h,
        "hourly_max_holes": max_holes,
        "transitions_rows": n_t,
    }


def fmt_stats(s):
    return (
        "  hourly_utilization: {rows} rows, dates {mn}..{mx}, "
        "max hole_count={mh}\n"
        "  state_transitions:  {tr} rows"
    ).format(
        rows=s["hourly_rows"], mn=s["hourly_min_date"],
        mx=s["hourly_max_date"], mh=s["hourly_max_holes"],
        tr=s["transitions_rows"],
    )


def main():
    execute = "--execute" in sys.argv

    settings = load_settings()
    db_path = get_db_path(settings)

    if not os.path.isfile(db_path):
        print("ERROR: DB not found at {}".format(db_path))
        sys.exit(1)

    print("DB:   {}".format(db_path))
    print("Mode: {}".format("EXECUTE (will commit)" if execute else "DRY RUN (will rollback)"))
    print("")

    # Connect with manual transaction control so we can rollback on dry run.
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    print("=== Before ===")
    print(fmt_stats(snapshot_stats(cur)))
    print("")

    total = 0
    print("=== Deletions ===")
    try:
        for label, sql in DELETES:
            cur.execute(sql)
            n = cur.rowcount
            print("  {}: {} rows".format(label, n))
            total += n

        if execute:
            conn.commit()
        else:
            conn.rollback()
    except Exception as e:
        conn.rollback()
        print("ERROR: {}; rolled back, no changes made.".format(e))
        sys.exit(1)

    print("")
    if execute and total > 0:
        # VACUUM cannot run inside a transaction.
        print("Running VACUUM to reclaim disk...")
        conn.isolation_level = None
        cur.execute("VACUUM")
        conn.isolation_level = ""

    print("=== After ===")
    print(fmt_stats(snapshot_stats(cur)))
    print("")
    if execute:
        print("Done. {} rows deleted.".format(total))
    else:
        print("Dry run: {} rows would be deleted.".format(total))
        print("Re-run with --execute to commit.")


if __name__ == "__main__":
    main()

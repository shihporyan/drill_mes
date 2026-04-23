"""Analyze accumulated tx1_event_latency and log_file_observe data.

Run after a few days of instrument operation (see notes/tx1_flush_latency_investigation.md).
Prints several tables to stdout and optionally writes a CSV bundle.

Usage:
    python tools/analyze_flush_latency.py
    DRILL_DEV_CONFIG=config/settings.dev.json python tools/analyze_flush_latency.py
    python tools/analyze_flush_latency.py --csv-out analysis_{YYYYMMDD}
    python tools/analyze_flush_latency.py --since 2026-04-23 --until 2026-05-07
"""

import argparse
import csv
import datetime
import os
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from parsers.base_parser import load_settings, get_db_path


def _where(since, until):
    """Build a WHERE fragment + params for an ISO-string timestamp column."""
    clauses = []
    params = []
    if since:
        clauses.append("{col} >= ?")
        params.append(since + "T00:00:00")
    if until:
        clauses.append("{col} < ?")
        params.append(until + "T00:00:00")
    return clauses, params


def _print_rows(title, headers, rows):
    print("\n=== {} ===".format(title))
    if not rows:
        print("(no data)")
        return
    widths = [len(h) for h in headers]
    for r in rows:
        for i, v in enumerate(r):
            widths[i] = max(widths[i], len(str(v)))
    fmt = "  ".join("{{:<{}}}".format(w) for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for r in rows:
        print(fmt.format(*[str(v) for v in r]))


def _fetch(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    headers = [c[0] for c in cursor.description]
    rows = cursor.fetchall()
    return headers, rows


def analyze(db_path, since=None, until=None, csv_out=None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    date_clauses, date_params = _where(since, until)
    lat_where = (" WHERE " + " AND ".join(c.format(col="detected_at") for c in date_clauses)) if date_clauses else ""
    obs_where = (" WHERE " + " AND ".join(c.format(col="observed_at") for c in date_clauses)) if date_clauses else ""

    reports = []

    # 1. Sample counts
    headers, rows = _fetch(conn,
        "SELECT COUNT(*) n_events, COUNT(DISTINCT machine_id) n_machines, "
        "MIN(detected_at) first, MAX(detected_at) last "
        "FROM tx1_event_latency" + lat_where, date_params)
    reports.append(("coverage", headers, rows))

    # 2. Overall latency per machine
    headers, rows = _fetch(conn,
        "SELECT machine_id, "
        "COUNT(*) n, "
        "ROUND(MIN(delay_seconds), 1) min_s, "
        "ROUND(AVG(delay_seconds), 1) avg_s, "
        "ROUND(MAX(delay_seconds), 1) max_s, "
        "SUM(wo_matched) n_wo "
        "FROM tx1_event_latency" + lat_where + " "
        "GROUP BY machine_id ORDER BY machine_id", date_params)
    reports.append(("latency_per_machine", headers, rows))

    # 3. Latency distribution buckets (per machine)
    headers, rows = _fetch(conn,
        "SELECT machine_id, "
        "SUM(CASE WHEN delay_seconds < 60 THEN 1 ELSE 0 END) lt_1min, "
        "SUM(CASE WHEN delay_seconds >= 60 AND delay_seconds < 600 THEN 1 ELSE 0 END) b_1_10min, "
        "SUM(CASE WHEN delay_seconds >= 600 AND delay_seconds < 1800 THEN 1 ELSE 0 END) b_10_30min, "
        "SUM(CASE WHEN delay_seconds >= 1800 AND delay_seconds < 3600 THEN 1 ELSE 0 END) b_30_60min, "
        "SUM(CASE WHEN delay_seconds >= 3600 THEN 1 ELSE 0 END) gt_1hr "
        "FROM tx1_event_latency" + lat_where + " "
        "GROUP BY machine_id ORDER BY machine_id", date_params)
    reports.append(("latency_buckets", headers, rows))

    # 4. Hour-of-day correlation
    headers, rows = _fetch(conn,
        "SELECT strftime('%H', detected_at) hour, "
        "COUNT(*) n, "
        "ROUND(AVG(delay_seconds), 1) avg_s, "
        "ROUND(MAX(delay_seconds), 1) max_s "
        "FROM tx1_event_latency" + lat_where + " "
        "GROUP BY hour ORDER BY hour", date_params)
    reports.append(("latency_by_hour", headers, rows))

    # 5. Size-growth pause detection per log type per machine.
    # Counts "freeze windows" where consecutive observations kept file_size
    # constant for > 5 minutes (suggests flush was paused).
    headers, rows = _fetch(conn,
        """
        WITH ordered AS (
          SELECT machine_id, log_type, observed_at, file_size,
                 LAG(file_size) OVER (PARTITION BY machine_id, log_type ORDER BY observed_at) prev_size,
                 LAG(observed_at) OVER (PARTITION BY machine_id, log_type ORDER BY observed_at) prev_at
          FROM log_file_observe
          WHERE file_size IS NOT NULL
          {where}
        )
        SELECT machine_id, log_type,
               COUNT(*) n_obs,
               SUM(CASE WHEN prev_size = file_size
                        AND (julianday(observed_at)-julianday(prev_at))*86400 > 300
                        THEN 1 ELSE 0 END) freezes_gt_5min
        FROM ordered
        GROUP BY machine_id, log_type
        ORDER BY machine_id, log_type
        """.format(where=("AND " + " AND ".join(c.format(col="observed_at") for c in date_clauses))
                   if date_clauses else ""),
        date_params)
    reports.append(("size_freezes_per_log", headers, rows))

    # 6. Cross-log freeze correlation per machine.
    # For each machine, count observations where TX1.Log's size was unchanged
    # but at least one other log type's size grew — the key evidence for
    # "only TX1 is delayed" (supports Notepad hypothesis H1).
    headers, rows = _fetch(conn,
        """
        WITH tx1 AS (
          SELECT machine_id, observed_at, file_size,
                 LAG(file_size) OVER (PARTITION BY machine_id ORDER BY observed_at) prev_size
          FROM log_file_observe WHERE log_type='TX1' AND file_size IS NOT NULL
        ),
        others AS (
          SELECT machine_id, observed_at, log_type, file_size,
                 LAG(file_size) OVER (PARTITION BY machine_id, log_type ORDER BY observed_at) prev_size
          FROM log_file_observe WHERE log_type != 'TX1' AND file_size IS NOT NULL
        )
        SELECT tx1.machine_id,
               SUM(CASE WHEN tx1.prev_size = tx1.file_size
                         AND others.prev_size < others.file_size
                         THEN 1 ELSE 0 END) tx1_frozen_others_grew,
               SUM(CASE WHEN tx1.prev_size = tx1.file_size
                         AND others.prev_size = others.file_size
                         THEN 1 ELSE 0 END) all_frozen,
               SUM(CASE WHEN tx1.prev_size < tx1.file_size THEN 1 ELSE 0 END) tx1_grew
        FROM tx1 JOIN others USING (machine_id, observed_at)
        GROUP BY tx1.machine_id
        ORDER BY tx1.machine_id
        """, ())
    reports.append(("tx1_vs_others", headers, rows))

    # Print
    for name, h, r in reports:
        _print_rows(name, h, r)

    # Optional CSV dump
    if csv_out:
        os.makedirs(csv_out, exist_ok=True)
        for name, h, r in reports:
            path = os.path.join(csv_out, name + ".csv")
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(h)
                for row in r:
                    w.writerow(list(row))
            print("wrote {}".format(path))

    conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=None, help="Path to drill_monitor.db (default: from settings)")
    ap.add_argument("--since", default=None, help="Include only events on/after YYYY-MM-DD")
    ap.add_argument("--until", default=None, help="Include only events before YYYY-MM-DD")
    ap.add_argument("--csv-out", default=None, help="Directory to dump CSVs (optional)")
    args = ap.parse_args()

    db_path = args.db or get_db_path(load_settings())
    if not os.path.exists(db_path):
        print("DB not found: {}".format(db_path), file=sys.stderr)
        sys.exit(1)
    print("Analyzing: {}".format(db_path))
    analyze(db_path, since=args.since, until=args.until, csv_out=args.csv_out)


if __name__ == "__main__":
    main()

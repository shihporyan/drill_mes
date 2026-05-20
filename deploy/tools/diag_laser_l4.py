"""One-shot diagnostic for the L4 cross-midnight RUN issue.

Run this ON the drill_monitor production box (the one that can see
C:\\DrillLogs and the live drill_monitor.db). It does NOT write anything —
it only prints the exact decision path the laser parser would take for L4
on a given date, plus whether the currently-installed code is the patched
version.

Usage (from the project / deploy root):
    python tools\\diag_laser_l4.py
    python tools\\diag_laser_l4.py --machine L4 --date 20260520
"""

import argparse
import datetime
import os
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from parsers.base_parser import load_settings, get_backup_root, get_db_path
import parsers.laser_log_parser as L


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--machine", default="L4")
    ap.add_argument("--date", default=datetime.date.today().strftime("%Y%m%d"),
                    help="YYYYMMDD; defaults to today")
    args = ap.parse_args()

    mid = args.machine
    today = datetime.datetime.strptime(args.date, "%Y%m%d").date()

    print("=" * 70)
    print("L4 cross-day diagnostic — machine=%s date=%s" % (mid, args.date))
    print("=" * 70)

    # --- 1. Is the installed code the patched version? ---
    has_walkback = hasattr(L, "find_active_cross_day_run_start")
    has_holecheck = hasattr(L, "_today_has_hole_events")
    print("\n[1] Installed laser_log_parser.py")
    print("    module file : %s" % L.__file__)
    print("    find_active_cross_day_run_start present: %s" % has_walkback)
    print("    _today_has_hole_events present         : %s" % has_holecheck)
    if not (has_walkback and has_holecheck):
        print("    >>> OLD CODE. The patched file is NOT what got loaded here.")
        print("    >>> Check you copied to the right path and restarted main.py.")
        return

    settings = load_settings()
    backup_root = get_backup_root(settings)
    db_path = get_db_path(settings)
    print("\n    backup_root : %s" % backup_root)
    print("    db_path     : %s" % db_path)

    # --- 2. Per-day ClsLaserCom content for the 3 relevant days ---
    print("\n[2] ClsLaserCom parse per day (intervals / leading_orphan_del)")
    for delta in (2, 1, 0):
        d = today - datetime.timedelta(days=delta)
        ds = d.strftime("%Y%m%d")
        log_dir = os.path.join(backup_root, mid, ds)
        f = L.find_log_file(log_dir, ds, "ClsLaserCom")
        if not f:
            print("    %s  ClsLaserCom: <FILE NOT FOUND> in %s" % (ds, log_dir))
            continue
        intervals, leading_del = L.parse_cls_laser_com(f)
        size = os.path.getsize(f)
        tail = "OPEN(%s)" % intervals[-1][0] if (intervals and intervals[-1][1] is None) else (
            "CLOSED" if intervals else "no-intervals")
        print("    %s  %s (%d bytes)" % (ds, os.path.basename(f), size))
        print("        intervals=%d  last=%s  leading_orphan_del=%s"
              % (len(intervals), tail, leading_del))

    # --- 3. Walk-back result for `today` ---
    print("\n[3] find_active_cross_day_run_start(%s)" % args.date)
    start = L.find_active_cross_day_run_start(backup_root, mid, today)
    print("    -> %s" % start)

    # --- 4. PLC hole corroboration for `today` ---
    log_dir = os.path.join(backup_root, mid, args.date)
    plc = L.find_log_file(log_dir, args.date, "ClsPLCTrd")
    print("\n[4] _today_has_hole_events(today ClsPLCTrd)")
    print("    plc file: %s" % plc)
    print("    -> %s" % (L._today_has_hole_events(plc) if plc else "no plc file"))

    # --- 5. What today's parse WOULD decide ---
    print("\n[5] Predicted parse outcome for %s" % args.date)
    run_intervals, leading_del = (L.parse_cls_laser_com(
        L.find_log_file(log_dir, args.date, "ClsLaserCom")) if
        L.find_log_file(log_dir, args.date, "ClsLaserCom") else ([], None))
    if not run_intervals and leading_del is None and start is not None and plc and L._today_has_hole_events(plc):
        print("    -> cross-day WOULD trigger: state=RUN since=%s" % start)
    else:
        print("    -> cross-day would NOT trigger. Reasons:")
        if run_intervals:
            print("       - today's ClsLaserCom has %d AUTO_RUN interval(s)" % len(run_intervals))
        if leading_del is not None:
            print("       - today's ClsLaserCom has a leading_orphan_del (%s)" % leading_del)
        if start is None:
            print("       - walk-back found no active cross-day RUN start")
        if plc and not L._today_has_hole_events(plc):
            print("       - no PLC hole events today (corroboration failed)")

    # --- 6. Live DB current state ---
    print("\n[6] machine_current_state in live DB")
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        r = conn.execute(
            "SELECT state, since, last_update FROM machine_current_state WHERE machine_id=?",
            (mid,)).fetchone()
        print("    %s" % (dict(r) if r else "<no row>"))
    except Exception as e:
        print("    DB read error: %s" % e)


if __name__ == "__main__":
    main()

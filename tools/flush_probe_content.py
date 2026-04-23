"""Per-line flush-latency probe: detect when each new TX1.Log line first
becomes visible on the SMB share.

Complements tools/flush_probe.py. `flush_probe.py` only tracks file
size/mtime (file-level); this script reads the full content every cycle
and records the exact timestamp when each new line first appears. That
gives event-level delay: `first_seen_at - line_timestamp`.

Usage:
    python tools/flush_probe_content.py --machine M13 --ip 10.10.1.23
    python tools/flush_probe_content.py --machine M13 --ip 10.10.1.23 --interval 15 --duration 7200
    python tools/flush_probe_content.py --machine M13 --ip 10.10.1.23 --fileop-only

Output:
    CSV at --out (default: flush_content_{machine}_{YYYYMMDD_HHMM}.csv)
    Columns: first_seen_at, machine_id, line_ts, delay_seconds, is_fileop, line

Stop: Ctrl+C, or wait for --duration to elapse.
"""

import argparse
import csv
import datetime
import os
import platform
import re
import signal
import sys
import time


LINE_TS_RE = re.compile(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d{3})")
FILEOP_RE = re.compile(r"FILEOPERATION.*OPERATION:\[")


def _parse_line_ts(line):
    """Extract ISO timestamp from the start of a TX1.Log line, or None."""
    m = LINE_TS_RE.match(line)
    if not m:
        return None
    raw = m.group(1)  # "2026/04/20 10:33:36.528"
    iso = raw.replace("/", "-", 2).replace(" ", "T", 1)
    return iso


def _read_remote(ip, day_prefix):
    """Read full remote TX1.Log over SMB. Returns (lines, error)."""
    filename = "{}TX1.Log".format(day_prefix)
    if platform.system() != "Windows":
        return None, "dev-env: smb unavailable"
    smb_path = "\\\\{}\\LOG\\{}".format(ip, filename)
    try:
        with open(smb_path, "r", encoding="cp932", errors="replace") as f:
            return f.read().splitlines(), None
    except (FileNotFoundError, OSError) as e:
        return None, str(e)[:200]


def run_probe(machine_id, ip, interval, duration, out_path, fileop_only):
    started = datetime.datetime.now()
    deadline = started + datetime.timedelta(seconds=duration) if duration else None

    print("Content probe: machine={} ip={} interval={}s duration={} fileop_only={}".format(
        machine_id, ip, interval,
        "infinite" if duration is None else "{}s".format(duration),
        fileop_only,
    ))
    print("Output: {}".format(out_path))
    print("Ctrl+C to stop.")

    stopping = {"flag": False}
    def _sigint(_sig, _frame):
        stopping["flag"] = True
        print("\nStop signal received, finishing current sample...")
    signal.signal(signal.SIGINT, _sigint)

    # Track all lines seen so far (by exact content match).
    # For 2h tests on ~85KB files this stays small (~few K entries).
    seen = set()
    first_read = True
    new_lines_count = 0

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "first_seen_at", "machine_id", "line_ts",
            "delay_seconds", "is_fileop", "line",
        ])

        while True:
            now = datetime.datetime.now()
            if deadline and now >= deadline:
                print("Duration reached.")
                break
            if stopping["flag"]:
                break

            day_prefix = now.strftime("%d")
            first_seen_at = now.isoformat()
            lines, error = _read_remote(ip, day_prefix)

            if lines is None:
                writer.writerow([first_seen_at, machine_id, "", "", "", "ERROR: " + (error or "")])
                f.flush()
            else:
                for line in lines:
                    if line in seen:
                        continue
                    seen.add(line)
                    if first_read:
                        # Don't count pre-existing lines as "newly seen"
                        continue
                    is_fileop = bool(FILEOP_RE.search(line))
                    if fileop_only and not is_fileop:
                        continue

                    line_ts = _parse_line_ts(line)
                    delay_s = ""
                    if line_ts:
                        try:
                            ts_dt = datetime.datetime.fromisoformat(line_ts)
                            delay_s = round((now - ts_dt).total_seconds(), 2)
                        except ValueError:
                            pass
                    writer.writerow([
                        first_seen_at, machine_id, line_ts or "",
                        delay_s, 1 if is_fileop else 0, line,
                    ])
                    new_lines_count += 1
                f.flush()

            if first_read:
                print("Baseline read: {} lines loaded into seen-set.".format(len(seen)))
                first_read = False
            else:
                print("  {} tick: total new lines so far = {} (seen set = {})".format(
                    now.strftime("%H:%M:%S"), new_lines_count, len(seen),
                ))

            elapsed = (datetime.datetime.now() - now).total_seconds()
            sleep_for = max(0, interval - elapsed)
            while sleep_for > 0 and not stopping["flag"]:
                step = min(1.0, sleep_for)
                time.sleep(step)
                sleep_for -= step

    print("Total new lines recorded: {}. File: {}".format(new_lines_count, out_path))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--machine", required=True, help="Machine ID, e.g. M13")
    ap.add_argument("--ip", required=True, help="Control PC IP, e.g. 10.10.1.23")
    ap.add_argument("--interval", type=float, default=30.0,
                    help="Seconds between reads (default: 30)")
    ap.add_argument("--duration", type=int, default=None,
                    help="Total run seconds (default: run until Ctrl+C)")
    ap.add_argument("--fileop-only", action="store_true",
                    help="Only record FILEOPERATION lines (smaller CSV)")
    ap.add_argument("--out", default=None,
                    help="CSV output path (default: flush_content_{machine}_{ts}.csv)")
    args = ap.parse_args()

    out_path = args.out or "flush_content_{}_{}.csv".format(
        args.machine, datetime.datetime.now().strftime("%Y%m%d_%H%M"),
    )

    run_probe(args.machine, args.ip, args.interval, args.duration,
              out_path, args.fileop_only)


if __name__ == "__main__":
    main()

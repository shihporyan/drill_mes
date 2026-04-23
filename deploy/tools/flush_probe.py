"""High-frequency SMB stat probe for TX1.Log flush-latency experiments.

For Method A (controlled Notepad open/close test) in notes/tx1_flush_latency_investigation.md.
The regular parser cycle runs every 600s — too coarse to see the effect of a
5-min Notepad open window. This probe polls `os.stat()` every N seconds and
writes CSV so you can plot size-over-time against the open/close schedule.

Usage:
    python tools/flush_probe.py --machine M13 --ip 10.10.1.23
    python tools/flush_probe.py --machine M13 --ip 10.10.1.23 --interval 15 --duration 2700
    python tools/flush_probe.py --machine M13 --ip 10.10.1.23 --log-type TX1,Drive

Output:
    CSV at --out (default: flush_probe_{machine}_{YYYYMMDD_HHMM}.csv)
    Columns: observed_at, machine_id, log_type, file_size, file_mtime, error

Stop: Ctrl+C, or wait for --duration to elapse.
"""

import argparse
import csv
import datetime
import os
import platform
import signal
import sys
import time


DEFAULT_LOG_TYPES = ["TX1", "Drive", "MACRO", "TARN", "FILE", "Alarm"]


def _stat_one(ip, day_prefix, log_type):
    """Return (size, mtime_iso, error_str) for one remote log file."""
    filename = "{}{}.Log".format(day_prefix, log_type)
    if platform.system() != "Windows":
        return None, None, "dev-env: smb unavailable"
    smb_path = "\\\\{}\\LOG\\{}".format(ip, filename)
    try:
        st = os.stat(smb_path)
        mtime_iso = datetime.datetime.fromtimestamp(st.st_mtime).isoformat()
        return st.st_size, mtime_iso, None
    except (FileNotFoundError, OSError) as e:
        return None, None, str(e)[:200]


def run_probe(machine_id, ip, log_types, interval, duration, out_path):
    """Poll os.stat on the remote SMB share every `interval` seconds.

    Writes one CSV row per (sample × log_type). Flushes every sample so you
    can tail -f the CSV during the experiment.
    """
    started = datetime.datetime.now()
    deadline = started + datetime.timedelta(seconds=duration) if duration else None

    print("Probe: machine={} ip={} interval={}s duration={} types={}".format(
        machine_id, ip, interval,
        "infinite" if duration is None else "{}s".format(duration),
        ",".join(log_types),
    ))
    print("Output: {}".format(out_path))
    print("Ctrl+C to stop.")

    # Graceful Ctrl+C
    stopping = {"flag": False}
    def _sigint(_sig, _frame):
        stopping["flag"] = True
        print("\nStop signal received, finishing current sample...")
    signal.signal(signal.SIGINT, _sigint)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "observed_at", "machine_id", "log_type",
            "file_size", "file_mtime", "error",
        ])

        sample_n = 0
        while True:
            now = datetime.datetime.now()
            if deadline and now >= deadline:
                print("Duration reached.")
                break
            if stopping["flag"]:
                break

            observed_at = now.isoformat()
            day_prefix = now.strftime("%d")
            for log_type in log_types:
                size, mtime_iso, error = _stat_one(ip, day_prefix, log_type)
                writer.writerow([
                    observed_at, machine_id, log_type,
                    size if size is not None else "",
                    mtime_iso or "",
                    error or "",
                ])
            f.flush()
            sample_n += 1
            if sample_n % 10 == 0:
                print("  {} samples written ({})".format(sample_n, observed_at))

            # Align next tick to wall clock
            elapsed = (datetime.datetime.now() - now).total_seconds()
            sleep_for = max(0, interval - elapsed)
            # Break sleep into 1s chunks so Ctrl+C is responsive
            while sleep_for > 0 and not stopping["flag"]:
                step = min(1.0, sleep_for)
                time.sleep(step)
                sleep_for -= step

    print("Total samples: {}. File: {}".format(sample_n, out_path))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--machine", required=True, help="Machine ID, e.g. M13")
    ap.add_argument("--ip", required=True, help="Control PC IP, e.g. 10.10.1.23")
    ap.add_argument("--interval", type=float, default=30.0,
                    help="Seconds between samples (default: 30)")
    ap.add_argument("--duration", type=int, default=None,
                    help="Total run seconds (default: run until Ctrl+C)")
    ap.add_argument("--log-type", default=",".join(DEFAULT_LOG_TYPES),
                    help="Comma-separated log types (default: all 6)")
    ap.add_argument("--out", default=None,
                    help="CSV output path (default: flush_probe_{machine}_{ts}.csv)")
    args = ap.parse_args()

    log_types = [t.strip() for t in args.log_type.split(",") if t.strip()]
    if not log_types:
        print("No log types specified.", file=sys.stderr)
        sys.exit(1)

    out_path = args.out or "flush_probe_{}_{}.csv".format(
        args.machine, datetime.datetime.now().strftime("%Y%m%d_%H%M"),
    )

    run_probe(args.machine, args.ip, log_types, args.interval, args.duration, out_path)


if __name__ == "__main__":
    main()

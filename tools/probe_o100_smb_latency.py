"""Long-running probe: measure latency between TX1 LoadProgram O100.txt
events and SMB-side mtime updates on the live O100.txt file.

Background: M01/M03/M17 are known to have SMB lazy mtime for *.Log files
(see project_smb_lazy_mtime.md). O100.txt is much smaller (~500B) and may
behave differently — but we need data before Phase 3 deploys, since the
M13-style extraction path depends on reading the live SMB file at TX1
event time.

Approach:
    - Every poll_secs (default 30): for each machine, stat SMB O100.txt
      → record (now_ts, machine, smb_mtime, size, content_hash if changed)
    - Every poll_secs: scan today's TX1.Log for new LoadProgram O100.txt
      events since last check → record (now_ts, machine, tx1_event_ts)
    - Write all observations to a CSV; offline analysis correlates events
      to mtime jumps and computes latency distribution.

Run on production for at least one full work day (8h+) to get meaningful
data. Stop with Ctrl-C.

Output: tools/probe_results/o100_smb_latency_{start_ts}.csv

Usage:
    python tools/probe_o100_smb_latency.py
    python tools/probe_o100_smb_latency.py --poll-secs 60
    python tools/probe_o100_smb_latency.py --smb-template "\\\\{ip}\\LOG\\Takeuchi\\NcProgram\\O100.txt"
"""

import argparse
import csv
import datetime
import hashlib
import os
import re
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from parsers.base_parser import (
    load_machines_config,
    load_settings,
    get_enabled_machines,
    get_backup_root,
)


# Match TX1 LoadProgram lines for O100.txt (CP932 prose included).
# Ex: 2026/05/02 07:51:50.104 ｙReadProgramｚLoadProgram(D:\Takeuchi\NcProgram\O100.txt )
TX1_LOAD_RE = re.compile(
    r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+.*LoadProgram\(([^)]+)\)"
)

# NcProgram is exposed as its own SMB share (separate from LOG share).
# Verified 2026-05-03 against M13-M18 — all 6 accessible at this path.
DEFAULT_SMB_TEMPLATE = r"\\{ip}\NcProgram\O100.txt"


def hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def stat_smb_o100(smb_path):
    """Return (mtime, size, hash) or None on error/missing."""
    try:
        st = os.stat(smb_path)
        return {
            "mtime": datetime.datetime.fromtimestamp(st.st_mtime),
            "size": st.st_size,
            "hash": hash_file(smb_path) if st.st_size > 0 else None,
        }
    except OSError as e:
        return {"error": str(e)}


def find_new_tx1_events(tx1_path, last_byte_offset):
    """Read TX1.Log starting from last_byte_offset; return new LoadProgram
    O100.txt events + new offset.

    Returns: (events_list, new_offset)
    Each event: dict with keys ts, path
    """
    if not os.path.isfile(tx1_path):
        return [], last_byte_offset
    size = os.path.getsize(tx1_path)
    if size <= last_byte_offset:
        return [], last_byte_offset

    events = []
    with open(tx1_path, "rb") as f:
        f.seek(last_byte_offset)
        chunk = f.read()
    text = chunk.decode("cp932", errors="replace")
    for line in text.splitlines():
        m = TX1_LOAD_RE.match(line)
        if not m:
            continue
        ts_str, path = m.group(1), m.group(2).strip()
        if not path.rstrip().lower().endswith("o100.txt"):
            continue
        try:
            ts = datetime.datetime.strptime(ts_str, "%Y/%m/%d %H:%M:%S.%f")
        except ValueError:
            continue
        events.append({"ts": ts, "path": path})
    return events, size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--poll-secs", type=int, default=30)
    ap.add_argument("--smb-template", default=DEFAULT_SMB_TEMPLATE,
                    help="Path template; {ip} is substituted per machine.")
    ap.add_argument("--machines", default="",
                    help="Comma-separated machine IDs to limit (default: all takeuchi). "
                         "Example: --machines M13,M14,M15,M16,M17,M18")
    args = ap.parse_args()

    settings = load_settings()
    backup_root = get_backup_root(settings)
    machines_cfg = load_machines_config()
    machines = [m for m in get_enabled_machines(machines_cfg)
                if m.get("type") == "takeuchi"]
    if args.machines:
        target_ids = set(s.strip() for s in args.machines.split(",") if s.strip())
        machines = [m for m in machines if m["id"] in target_ids]

    start_ts = datetime.datetime.now()
    out_dir = os.path.join(PROJECT_ROOT, "tools", "probe_results")
    os.makedirs(out_dir, exist_ok=True)
    out_csv = os.path.join(out_dir, "o100_smb_latency_{}.csv".format(
        start_ts.strftime("%Y%m%d_%H%M%S")))

    print("=== O100.txt SMB latency probe ===")
    print("start: {}".format(start_ts.isoformat()))
    print("poll:  every {}s".format(args.poll_secs))
    print("smb:   {}".format(args.smb_template))
    print("out:   {}".format(out_csv))
    print("(Ctrl-C to stop)")
    print()

    csv_f = open(out_csv, "w", newline="")
    writer = csv.DictWriter(csv_f, fieldnames=[
        "obs_ts", "machine", "obs_type",
        "tx1_event_ts", "smb_mtime", "smb_size", "smb_hash",
        "latency_secs", "error"
    ])
    writer.writeheader()
    csv_f.flush()

    # Per-machine state
    state = {}
    today = datetime.date.today()
    for m in machines:
        mid = m["id"]
        state[mid] = {
            "tx1_offset": 0,
            "last_smb_hash": None,
            "last_smb_mtime": None,
        }

    try:
        while True:
            now = datetime.datetime.now()

            # Roll over TX1 offset at midnight
            if now.date() != today:
                today = now.date()
                for s in state.values():
                    s["tx1_offset"] = 0

            for m in machines:
                mid = m["id"]
                ip = m.get("ip", "")
                smb_path = args.smb_template.format(ip=ip)

                # 1. Stat SMB O100.txt
                obs = stat_smb_o100(smb_path)
                if "error" in obs:
                    writer.writerow({
                        "obs_ts": now.isoformat(),
                        "machine": mid,
                        "obs_type": "smb_error",
                        "error": obs["error"],
                    })
                else:
                    changed = (obs["hash"] != state[mid]["last_smb_hash"])
                    writer.writerow({
                        "obs_ts": now.isoformat(),
                        "machine": mid,
                        "obs_type": "smb_change" if changed else "smb_poll",
                        "smb_mtime": obs["mtime"].isoformat(),
                        "smb_size": obs["size"],
                        "smb_hash": obs["hash"],
                    })
                    if changed:
                        print("[{}] {} SMB CHANGE  size={} mtime={} hash={}".format(
                            now.strftime("%H:%M:%S"), mid, obs["size"],
                            obs["mtime"].strftime("%H:%M:%S"), obs["hash"]))
                    state[mid]["last_smb_hash"] = obs["hash"]
                    state[mid]["last_smb_mtime"] = obs["mtime"]

                # 2. Scan today's TX1 for new LoadProgram events
                date_dir = today.strftime("%Y%m%d")
                dd = today.strftime("%d")
                tx1_path = os.path.join(backup_root, mid, date_dir,
                                        "{}TX1.Log".format(dd))
                events, new_offset = find_new_tx1_events(
                    tx1_path, state[mid]["tx1_offset"])
                state[mid]["tx1_offset"] = new_offset
                for ev in events:
                    last_mtime = state[mid]["last_smb_mtime"]
                    latency = None
                    if last_mtime:
                        latency = (last_mtime - ev["ts"]).total_seconds()
                    writer.writerow({
                        "obs_ts": now.isoformat(),
                        "machine": mid,
                        "obs_type": "tx1_event",
                        "tx1_event_ts": ev["ts"].isoformat(),
                        "smb_mtime": last_mtime.isoformat() if last_mtime else "",
                        "latency_secs": "{:.3f}".format(latency) if latency is not None else "",
                    })
                    print("[{}] {} TX1 EVENT @ {}  latency_to_last_smb={}".format(
                        now.strftime("%H:%M:%S"), mid,
                        ev["ts"].strftime("%H:%M:%S"),
                        "{:.1f}s".format(latency) if latency else "n/a"))

            csv_f.flush()
            time.sleep(args.poll_secs)

    except KeyboardInterrupt:
        print()
        print("Stopped. CSV: {}".format(out_csv))
    finally:
        csv_f.close()


if __name__ == "__main__":
    main()

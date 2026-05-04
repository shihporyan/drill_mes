"""One-shot recon: test NcProgram SMB accessibility + capture O100.txt snapshot.

User opened NcProgram share read perms for M13-M18 (5/3). Before any
recurring poll, we need to verify per-machine which paths actually work and
which were missed in the share config.

Conservative by design — production machines are running:
    - Single-shot, exits when done (no polling, no continuous load)
    - Per machine: 1 listdir + 1 stat + 1 read (~500B file)
    - Total network footprint: trivial (~3KB)
    - Failure on one machine does NOT block others — accumulates a report
    - Read-only operations only (os.listdir, os.stat, open 'rb')

LOG collector compatibility: this probe accesses NcProgram subdir, the LOG
collector accesses date dirs (YYMMDD\\). Different paths, no conflict.
NO need to pause the LOG collector.

Output:
    stdout summary table + tools/probe_results/o100_ncprogram_access_{ts}.csv

Usage:
    python tools/probe_o100_ncprogram_access.py
    python tools/probe_o100_ncprogram_access.py --machines M13,M14
    python tools/probe_o100_ncprogram_access.py --share-template "\\\\{ip}\\NcProgram"
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
    get_enabled_machines,
)
from parsers.o100_parser import parse_o100_content


# NcProgram is exposed as its own SMB share (separate from LOG share).
# Verified 2026-05-03 against M13-M18 — all 6 machines accessible at this path.
DEFAULT_SHARE_TEMPLATE = r"\\{ip}\NcProgram"

# User stated M13-M18 NcProgram share perms were opened on 5/3.
DEFAULT_MACHINES = "M13,M14,M15,M16,M17,M18"


def probe_machine(ip, share_template):
    """Single-machine recon. Returns dict of observations.

    Ordered checks (each step gated on previous success):
        1. listdir(NcProgram) — verifies share access + dir exists
        2. stat(O100.txt) — verifies file exists + readable metadata
        3. read+parse O100.txt — verifies content readable + extracts active_subs

    Any OSError stops the chain and is recorded in 'error'.
    """
    nc_dir = share_template.format(ip=ip)
    o100_path = os.path.join(nc_dir, "O100.txt")

    result = {
        "ip": ip,
        "nc_dir_path": nc_dir,
        "dir_accessible": False,
        "dir_entry_count": None,
        "o100_exists": False,
        "o100_size": None,
        "o100_mtime": None,
        "o100_hash": None,
        "active_subs": None,
        "error": None,
    }

    # Step 1: list dir
    try:
        listing = os.listdir(nc_dir)
        result["dir_accessible"] = True
        result["dir_entry_count"] = len(listing)
    except OSError as e:
        result["error"] = "listdir: {}".format(e)
        return result

    # Step 2: stat O100.txt
    try:
        st = os.stat(o100_path)
        result["o100_exists"] = True
        result["o100_size"] = st.st_size
        result["o100_mtime"] = datetime.datetime.fromtimestamp(
            st.st_mtime).isoformat(timespec="seconds")
    except OSError as e:
        result["error"] = "stat O100.txt: {}".format(e)
        return result

    # Step 3: read + parse (~500B, safe single read)
    try:
        with open(o100_path, "rb") as f:
            raw = f.read()
        text = raw.decode("cp932", errors="replace")
        parsed = parse_o100_content(text)
        result["o100_hash"] = parsed["content_hash"][:12]
        result["active_subs"] = parsed["active_subs"]
    except OSError as e:
        result["error"] = "read O100.txt: {}".format(e)

    return result


def fmt_subs(subs):
    if subs is None:
        return None
    if not subs:
        return "[]"
    return "[" + ",".join(str(s) for s in subs) + "]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--machines", default=DEFAULT_MACHINES,
                    help="Comma-separated machine IDs (default: M13-M18)")
    ap.add_argument("--share-template", default=DEFAULT_SHARE_TEMPLATE,
                    help=r"SMB path template; {ip} is substituted (default: \\{ip}\LOG\Takeuchi\NcProgram)")
    args = ap.parse_args()

    target_ids = set(s.strip() for s in args.machines.split(",") if s.strip())

    machines_cfg = load_machines_config()
    machines = [m for m in get_enabled_machines(machines_cfg)
                if m["id"] in target_ids]
    if not machines:
        print("ERROR: no matching machines in config")
        return

    print("=== NcProgram SMB access probe ===")
    print("template: {}".format(args.share_template))
    print("targets:  {}".format(", ".join(m["id"] for m in machines)))
    print()

    results = []
    for m in machines:
        mid = m["id"]
        ip = m.get("ip", "")
        print("Probing {} ({})...".format(mid, ip), end=" ", flush=True)
        r = probe_machine(ip, args.share_template)
        r["machine"] = mid
        results.append(r)
        if r["dir_accessible"] and r["o100_exists"] and r["active_subs"] is not None:
            print("OK  active_subs={}".format(fmt_subs(r["active_subs"])))
        elif r["dir_accessible"]:
            print("DIR OK / O100 problem: {}".format(r["error"]))
        else:
            print("BLOCKED ({})".format(r["error"]))

    # Summary table
    print()
    print("{:<5}  {:<14}  {:<8}  {:<8}  {:>6}  {:<19}  {:<14}  active_subs / error".format(
        "M", "ip", "dir", "o100", "size", "mtime", "hash"))
    print("-" * 130)
    for r in results:
        dir_str = "yes" if r["dir_accessible"] else "NO"
        o100_str = "yes" if r["o100_exists"] else "NO"
        size = str(r["o100_size"]) if r["o100_size"] is not None else "-"
        mtime = (r["o100_mtime"] or "-")
        h = r["o100_hash"] or "-"
        if r["active_subs"] is not None:
            tail = fmt_subs(r["active_subs"])
        elif r["error"]:
            tail = "ERROR: " + r["error"][:80]
        else:
            tail = "-"
        print("{:<5}  {:<14}  {:<8}  {:<8}  {:>6}  {:<19}  {:<14}  {}".format(
            r["machine"], r["ip"], dir_str, o100_str, size, mtime, h, tail))

    # Counts
    print()
    n_ok = sum(1 for r in results if r["active_subs"] is not None)
    n_dir_only = sum(1 for r in results
                     if r["dir_accessible"] and r["active_subs"] is None)
    n_blocked = sum(1 for r in results if not r["dir_accessible"])
    print("Summary: {} fully accessible / {} dir-only / {} blocked".format(
        n_ok, n_dir_only, n_blocked))

    # CSV output
    out_dir = os.path.join(PROJECT_ROOT, "tools", "probe_results")
    os.makedirs(out_dir, exist_ok=True)
    out_csv = os.path.join(out_dir, "o100_ncprogram_access_{}.csv".format(
        datetime.datetime.now().strftime("%Y%m%d_%H%M%S")))
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "machine", "ip", "nc_dir_path", "dir_accessible", "dir_entry_count",
            "o100_exists", "o100_size", "o100_mtime", "o100_hash",
            "active_subs", "error",
        ])
        w.writeheader()
        for r in results:
            row = dict(r)
            row["active_subs"] = fmt_subs(r["active_subs"]) if r["active_subs"] is not None else ""
            w.writerow(row)
    print("CSV: {}".format(out_csv))


if __name__ == "__main__":
    main()

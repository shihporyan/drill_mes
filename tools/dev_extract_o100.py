"""Dev-mode O100.txt extraction & verification driver.

Walks two sample directories (M14_0503 / M13_0503) and exercises both
extraction paths to verify the parser + cross-validate the two sources:

    Mode A (M14): scan FILE.Log → extract every dump block → parse content
    Mode B (M13): scan TX1.Log → list LoadProgram events → parse current snapshot

Output: per-machine timeline of (timestamp, source, active_subs, content_hash).

Run:
    python3 tools/dev_extract_o100.py

Reference: notes/mech_drill_board_identification.md
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from parsers.o100_parser import (
    parse_o100_content,
    extract_dumps_from_file_log,
    find_load_events_in_tx1,
)


VERIFY_ROOT = os.path.join(PROJECT_ROOT, "original_logs", "verify")


def find_log(machine_dir, day_prefix, log_name):
    """Locate a {day_prefix}{log_name} file under machine_dir."""
    candidate = os.path.join(machine_dir, "{}{}".format(day_prefix, log_name))
    return candidate if os.path.isfile(candidate) else None


def find_o100_snapshot(machine_dir):
    """Find the live O100.txt snapshot. M14 puts it under NcProgram/, M13
    sometimes at top level depending on collection method."""
    candidates = [
        os.path.join(machine_dir, "NcProgram", "O100.txt"),
        os.path.join(machine_dir, "O100.txt"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def short_hash(h):
    return h[:12] if h else "-"


def fmt_subs(subs):
    if not subs:
        return "(none)"
    return "[" + ", ".join(str(s) for s in subs) + "]"


def process_file_log_mode(machine_id, file_log_path):
    """M14-style: extract all dumps from FILE.Log."""
    print()
    print("--- {} Mode A (FILE.Log dumps) ---".format(machine_id))
    print("    source: {}".format(os.path.relpath(file_log_path, PROJECT_ROOT)))

    dumps = extract_dumps_from_file_log(file_log_path)
    if not dumps:
        print("    (no O100.txt dumps found)")
        return []

    rows = []
    last_hash = None
    for d in dumps:
        ts = d["timestamp"].strftime("%Y/%m/%d %H:%M:%S.%f")[:-3]
        subs = d["parsed"]["active_subs"]
        h = d["parsed"]["content_hash"]
        change_marker = "  *CHANGE*" if last_hash and h != last_hash else ""
        print("    {}  active_subs={:30s}  hash={}{}".format(
            ts, fmt_subs(subs), short_hash(h), change_marker))
        last_hash = h
        rows.append({
            "machine": machine_id,
            "timestamp": d["timestamp"],
            "source": "file_log",
            "active_subs": subs,
            "content_hash": h,
        })
    return rows


def process_tx1_mode(machine_id, tx1_paths, snapshot_path):
    """M13-style: list TX1 LoadProgram events; parse the live snapshot we have.

    In dev we only have ONE snapshot (latest), so all timestamps map to the
    same parsed content. In production this would call SMB read at each event.
    """
    print()
    print("--- {} Mode B (TX1 events + live snapshot) ---".format(machine_id))
    for p in tx1_paths:
        print("    tx1 source: {}".format(os.path.relpath(p, PROJECT_ROOT)))
    if snapshot_path:
        print("    snapshot:  {}".format(os.path.relpath(snapshot_path, PROJECT_ROOT)))
    else:
        print("    snapshot:  (NONE — cannot verify content)")

    events = []
    for tx1_path in tx1_paths:
        events.extend(find_load_events_in_tx1(tx1_path))
    events.sort(key=lambda e: e["timestamp"])

    if not events:
        print("    (no O100.txt LoadProgram events found in TX1)")
        return []

    snapshot_parsed = None
    if snapshot_path:
        with open(snapshot_path, "rb") as f:
            content = f.read().decode("cp932", errors="replace")
        snapshot_parsed = parse_o100_content(content)
        print("    snapshot parsed: active_subs={}  hash={}".format(
            fmt_subs(snapshot_parsed["active_subs"]),
            short_hash(snapshot_parsed["content_hash"]),
        ))

    rows = []
    for e in events:
        ts = e["timestamp"].strftime("%Y/%m/%d %H:%M:%S.%f")[:-3]
        if snapshot_parsed:
            subs = snapshot_parsed["active_subs"]
            h = snapshot_parsed["content_hash"]
            note = "(snapshot)"
        else:
            subs = []
            h = None
            note = "(no snapshot)"
        print("    {}  active_subs={:30s}  hash={}  {}".format(
            ts, fmt_subs(subs), short_hash(h), note))
        rows.append({
            "machine": machine_id,
            "timestamp": e["timestamp"],
            "source": "tx1_event+live_snapshot",
            "active_subs": subs,
            "content_hash": h,
        })
    return rows


def cross_check_a_vs_b(machine_id, file_log_path, snapshot_path):
    """For M14: confirm the LAST FILE.Log dump matches the live snapshot."""
    if not (file_log_path and snapshot_path):
        return
    print()
    print("--- {} Cross-check: last FILE.Log dump vs live snapshot ---".format(machine_id))

    dumps = extract_dumps_from_file_log(file_log_path)
    if not dumps:
        print("    (no dumps to compare)")
        return

    last_dump = dumps[-1]
    with open(snapshot_path, "rb") as f:
        live_content = f.read().decode("cp932", errors="replace")
    live_parsed = parse_o100_content(live_content)

    last_hash = last_dump["parsed"]["content_hash"]
    live_hash = live_parsed["content_hash"]
    match = "✅ MATCH" if last_hash == live_hash else "❌ MISMATCH"
    print("    last dump @ {}  hash={}".format(
        last_dump["timestamp"].strftime("%Y/%m/%d %H:%M:%S"),
        short_hash(last_hash)))
    print("    live snapshot         hash={}".format(short_hash(live_hash)))
    print("    {}  active_subs match: {}".format(
        match, last_dump["parsed"]["active_subs"] == live_parsed["active_subs"]))


def process_machine(machine_id, machine_dir):
    print("=" * 70)
    print("Machine: {}".format(machine_id))
    print("Path:    {}".format(os.path.relpath(machine_dir, PROJECT_ROOT)))

    snapshot_path = find_o100_snapshot(machine_dir)

    # Find available days (FILE.Log + TX1.Log) by scanning prefixes
    file_logs = []
    tx1_logs = []
    if os.path.isdir(machine_dir):
        for fname in sorted(os.listdir(machine_dir)):
            if fname.endswith("FILE.Log"):
                file_logs.append(os.path.join(machine_dir, fname))
            elif fname.endswith("TX1.Log"):
                tx1_logs.append(os.path.join(machine_dir, fname))

    rows = []

    # Mode A — pick FILE.Log files large enough to potentially contain dumps.
    # M13-style FILE.Log is ~150 bytes (Copy events only); M14 dumps are MB-scale.
    for flog in file_logs:
        size = os.path.getsize(flog)
        if size < 1024:
            print()
            print("--- {} FILE.Log too small ({}B) — likely no content dumps ---".format(
                machine_id, size))
            print("    skip: {}".format(os.path.relpath(flog, PROJECT_ROOT)))
            continue
        rows.extend(process_file_log_mode(machine_id, flog))

    # Mode B — TX1 events (always run; this is the universal path)
    rows.extend(process_tx1_mode(machine_id, tx1_logs, snapshot_path))

    # Cross-check (M14 only meaningful)
    big_file_logs = [f for f in file_logs if os.path.getsize(f) >= 1024]
    if big_file_logs and snapshot_path:
        # Cross-check against the most recent (largest day prefix) FILE.Log
        cross_check_a_vs_b(machine_id, big_file_logs[-1], snapshot_path)

    return rows


def main():
    machine_dirs = [
        ("M14", os.path.join(VERIFY_ROOT, "M14_0503")),
        ("M13", os.path.join(VERIFY_ROOT, "M13_0503")),
    ]

    all_rows = []
    for machine_id, mdir in machine_dirs:
        if not os.path.isdir(mdir):
            print("WARN: {} dir not found: {}".format(machine_id, mdir))
            continue
        all_rows.extend(process_machine(machine_id, mdir))
        print()

    # Summary
    print("=" * 70)
    print("Summary: {} total events across {} machines".format(
        len(all_rows), len(machine_dirs)))
    by_source = {}
    for r in all_rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    for src, n in by_source.items():
        print("  {}: {}".format(src, n))


if __name__ == "__main__":
    main()

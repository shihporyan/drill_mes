"""Analyze the output of probe_o100_smb_latency.py.

Reads CSV and answers Phase 4 Layer 1 questions:
    Q1. Per-machine activity counts (polls / changes / events / errors)
    Q2. SMB content change timeline per machine
    Q3. TX1 LoadProgram events per machine (filtered to probe window)
    Q4. For each TX1 event: did SMB content actually change around it?
    Q5. For each SMB content change: was there a nearby TX1 event?
    Q6. SMB mtime vs TX1 event_ts after JST→TST correction

Two data quality fixes applied (5/4 discovery):
    - Filter TX1 events to only those WITHIN probe window (drops stale
      ##TX1.Log content from monthly day-prefix overlap, e.g. 04TX1.Log
      shared across April-04 and May-04 directories)
    - Subtract TX1_TIME_OFFSET_HOURS from TX1 event timestamps before
      comparing to SMB mtime. Takeuchi machines write TX1.Log in JST
      (UTC+9), filesystem mtime in local TST (UTC+8). Without this
      correction, every event looks "1h late" (false positive lazy mtime).

Usage:
    python tools/analyze_o100_probe.py <csv_path>
"""

import csv
import datetime
import sys
from collections import defaultdict


# Window for matching TX1 event ↔ SMB change "nearby"
NEARBY_WINDOW_SECONDS = 300   # 5 min: generous, change may appear in next poll

# Takeuchi TX1.Log writes timestamps in JST (UTC+9); filesystem mtime is
# stored in local TST (UTC+8). Subtract this to align TX1 events to "real"
# time before comparing with SMB mtime.
TX1_TIME_OFFSET_HOURS = 1


def parse_ts(s):
    if not s:
        return None
    return datetime.datetime.fromisoformat(s)


def load_csv(path):
    """Returns list of dicts with parsed timestamps."""
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append({
                "obs_ts": parse_ts(r["obs_ts"]),
                "machine": r["machine"],
                "obs_type": r["obs_type"],
                "tx1_event_ts": parse_ts(r["tx1_event_ts"]),
                "smb_mtime": parse_ts(r["smb_mtime"]),
                "smb_size": int(r["smb_size"]) if r["smb_size"] else None,
                "smb_hash": r["smb_hash"] or None,
                "latency_secs": float(r["latency_secs"]) if r["latency_secs"] else None,
                "error": r["error"] or None,
            })
    return rows


def split_by_machine(rows):
    by_m = defaultdict(list)
    for r in rows:
        by_m[r["machine"]].append(r)
    return by_m


def basic_counts(rows):
    c = defaultdict(int)
    for r in rows:
        c[r["obs_type"]] += 1
    return dict(c)


def collect_smb_changes(rows):
    """Return chronological list of distinct hash changes for one machine.
    Filters to obs_type=smb_change (probe already did dedup)."""
    return [r for r in rows if r["obs_type"] == "smb_change"]


def collect_tx1_events(rows, probe_start=None, probe_end=None):
    """Return chronological deduped list of TX1 LoadProgram events.

    Dedups by tx1_event_ts (probe re-emits same event each poll until
    accumulated). Also adds 'tx1_event_ts_corrected' = JST→TST conversion.

    If probe_start/probe_end given, filters to events whose corrected
    timestamp falls within [probe_start, probe_end] — drops stale events
    from monthly day-prefix overlap (e.g. April-04 events appearing in
    May-04 directory).
    """
    seen = set()
    out = []
    for r in rows:
        if r["obs_type"] != "tx1_event":
            continue
        key = r["tx1_event_ts"]
        if key in seen:
            continue
        seen.add(key)
        # JST→TST correction
        corrected = r["tx1_event_ts"] - datetime.timedelta(hours=TX1_TIME_OFFSET_HOURS)
        r = dict(r)
        r["tx1_event_ts_corrected"] = corrected
        out.append(r)
    out.sort(key=lambda x: x["tx1_event_ts"])

    if probe_start and probe_end:
        out = [e for e in out
               if probe_start <= e["tx1_event_ts_corrected"] <= probe_end]
    return out


def smb_state_at(rows, t):
    """Return the most recent SMB observation (any type) at or before time t.
    Used to know the SMB-known content at a given point."""
    best = None
    for r in rows:
        if r["obs_type"] not in ("smb_poll", "smb_change"):
            continue
        if r["obs_ts"] > t:
            break
        if r["smb_hash"] or r["smb_mtime"]:
            best = r
    return best


def find_first_change_after(changes, t):
    """First smb_change row after time t (or None)."""
    for c in changes:
        if c["obs_ts"] > t:
            return c
    return None


def find_last_change_before(changes, t):
    """Last smb_change row at or before time t (or None)."""
    best = None
    for c in changes:
        if c["obs_ts"] > t:
            break
        best = c
    return best


def analyze_machine(mid, rows, probe_start, probe_end):
    print()
    print("=" * 70)
    print("Machine: {}".format(mid))
    print("=" * 70)

    counts = basic_counts(rows)
    total = sum(counts.values())
    print("\nQ1. Activity counts (total {})".format(total))
    for k in ("smb_poll", "smb_change", "tx1_event", "smb_error"):
        print("  {:<12}  {}".format(k, counts.get(k, 0)))

    changes = collect_smb_changes(rows)
    events_all = collect_tx1_events(rows)
    events = collect_tx1_events(rows, probe_start, probe_end)
    n_dropped = len(events_all) - len(events)

    print("\nQ2. SMB content changes ({} distinct hash transitions)".format(len(changes)))
    for c in changes[:30]:
        print("  obs={}  mtime={}  size={:>5}  hash={}".format(
            c["obs_ts"].strftime("%m/%d %H:%M:%S"),
            c["smb_mtime"].strftime("%m/%d %H:%M:%S") if c["smb_mtime"] else "-",
            c["smb_size"] if c["smb_size"] else "-",
            c["smb_hash"]))
    if len(changes) > 30:
        print("  ... ({} more)".format(len(changes) - 30))

    print("\nQ3. TX1 LoadProgram events ({} in probe window; {} stale dropped)".format(
        len(events), n_dropped))
    print("    (timestamps shown JST→TST corrected)")
    for e in events[:30]:
        print("  event_ts={}  (raw {}  +1h JST)".format(
            e["tx1_event_ts_corrected"].strftime("%m/%d %H:%M:%S"),
            e["tx1_event_ts"].strftime("%H:%M:%S")))
    if len(events) > 30:
        print("  ... ({} more)".format(len(events) - 30))

    # Q4: TX1 events that changed content vs not
    print("\nQ4. TX1 events vs SMB content change correlation (window ±{}s)".format(
        NEARBY_WINDOW_SECONDS))
    changed_by_load = 0
    unchanged_by_load = 0
    for e in events:
        et = e["tx1_event_ts_corrected"]
        # Was there an SMB change with mtime close to corrected event time?
        smb_changed = any(c["smb_mtime"] and
                          et - datetime.timedelta(seconds=NEARBY_WINDOW_SECONDS) <=
                          c["smb_mtime"] <=
                          et + datetime.timedelta(seconds=NEARBY_WINDOW_SECONDS)
                          for c in changes)
        if smb_changed:
            changed_by_load += 1
        else:
            unchanged_by_load += 1
    print("  events that changed content : {}".format(changed_by_load))
    print("  events with no content change: {}  (re-LOADs of unchanged file)".format(unchanged_by_load))

    # Q5: SMB changes without nearby TX1 event (orphan edits)
    print("\nQ5. SMB content changes vs TX1 event correlation (window ±{}s)".format(
        NEARBY_WINDOW_SECONDS))
    triggered = 0
    orphans = 0
    for c in changes:
        if not c["smb_mtime"]:
            continue
        mt = c["smb_mtime"]
        nearby_event = any(
            mt - datetime.timedelta(seconds=NEARBY_WINDOW_SECONDS) <=
            e["tx1_event_ts_corrected"] <=
            mt + datetime.timedelta(seconds=NEARBY_WINDOW_SECONDS)
            for e in events
        )
        if nearby_event:
            triggered += 1
        else:
            orphans += 1
    print("  changes with nearby TX1 event   : {}".format(triggered))
    print("  changes WITHOUT TX1 event nearby: {}  (operator edited but didn't LOAD)".format(orphans))

    # Q6: SMB mtime relative to corrected TX1 event time
    print("\nQ6. SMB mtime vs TX1 event_ts_corrected  (delta = mtime - event_ts)")
    print("    After JST→TST correction; ≈0 = real-time, >0 = SMB lazy")
    deltas = []
    for e in events:
        if e["smb_mtime"] and e["tx1_event_ts_corrected"]:
            d = (e["smb_mtime"] - e["tx1_event_ts_corrected"]).total_seconds()
            deltas.append(d)
    if deltas:
        deltas_sorted = sorted(deltas)
        n = len(deltas)
        print("  count: {}".format(n))
        print("  min:   {:>10.1f}s  ({})".format(deltas_sorted[0], fmt_dur(deltas_sorted[0])))
        print("  p25:   {:>10.1f}s  ({})".format(deltas_sorted[n//4], fmt_dur(deltas_sorted[n//4])))
        print("  p50:   {:>10.1f}s  ({})".format(deltas_sorted[n//2], fmt_dur(deltas_sorted[n//2])))
        print("  p75:   {:>10.1f}s  ({})".format(deltas_sorted[3*n//4], fmt_dur(deltas_sorted[3*n//4])))
        print("  max:   {:>10.1f}s  ({})".format(deltas_sorted[-1], fmt_dur(deltas_sorted[-1])))

        very_lazy = [d for d in deltas if d < -60]   # mtime > 1 min behind event
        if very_lazy:
            print("  ⚠️  {} events with mtime >60s BEHIND event (real lazy mtime?)".format(
                len(very_lazy)))


def fmt_dur(secs):
    """Format seconds as a human-readable duration."""
    if abs(secs) < 60:
        return "{:.1f}s".format(secs)
    if abs(secs) < 3600:
        return "{:.1f}min".format(secs / 60)
    if abs(secs) < 86400:
        return "{:.1f}h".format(secs / 3600)
    return "{:.1f}d".format(secs / 86400)


def overall_summary(rows, by_m, probe_start, probe_end):
    if not rows:
        return
    duration = (probe_end - probe_start).total_seconds()

    print("=" * 70)
    print("OVERALL SUMMARY")
    print("=" * 70)
    print("CSV row count   : {}".format(len(rows)))
    print("Probe duration  : {}".format(fmt_dur(duration)))
    print("                  {} → {}".format(
        probe_start.strftime("%Y-%m-%d %H:%M:%S"),
        probe_end.strftime("%Y-%m-%d %H:%M:%S")))
    print("Machines        : {}".format(", ".join(sorted(by_m.keys()))))
    print()
    print("{:<5}  {:>8}  {:>8}  {:>12}  {:>10}  {:>8}".format(
        "M", "polls", "changes", "tx1(window)", "tx1(stale)", "errors"))
    print("-" * 65)
    for mid in sorted(by_m.keys()):
        c = basic_counts(by_m[mid])
        events_in = len(collect_tx1_events(by_m[mid], probe_start, probe_end))
        events_all = len(collect_tx1_events(by_m[mid]))
        print("{:<5}  {:>8}  {:>8}  {:>12}  {:>10}  {:>8}".format(
            mid, c.get("smb_poll", 0), c.get("smb_change", 0),
            events_in, events_all - events_in, c.get("smb_error", 0)))


def main():
    if len(sys.argv) < 2:
        print("usage: python tools/analyze_o100_probe.py <csv_path>")
        return
    path = sys.argv[1]
    rows = load_csv(path)
    by_m = split_by_machine(rows)

    probe_start = min(r["obs_ts"] for r in rows)
    probe_end = max(r["obs_ts"] for r in rows)

    overall_summary(rows, by_m, probe_start, probe_end)
    for mid in sorted(by_m.keys()):
        analyze_machine(mid, by_m[mid], probe_start, probe_end)


if __name__ == "__main__":
    main()

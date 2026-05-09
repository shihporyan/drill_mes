"""Copy raw machine logs + DB snapshot + app log to USB for off-site verification.

Run on the production compute PC. Collects enough data so hole count,
hourly_utilization, work-order assignment, and laser shift averages can
all be cross-verified against raw logs on the dev machine.

Usage:
    python tools/usb_sample.py                    # Auto-detect USB, default 2-day window
    python tools/usb_sample.py D:                 # Explicit drive letter
    python tools/usb_sample.py D:\\subdir         # Explicit target path
    python tools/usb_sample.py --audit            # Wide window for full audit
    python tools/usb_sample.py D: --audit         # Both

Window control:
    Default               Drive=2d, TX1=2d, FILE=2d, Kataoka=2d  (~50 MB)
    --audit               Drive=2d, TX1=8d, FILE=8d, Kataoka=2d  (~500 MB-1 GB)
    --tx1-days N          Override TX1 window
    --file-days N         Override FILE window
    --drive-days N        Override Drive window
    --kataoka-days N      Override Kataoka ClsPLCTrd window

Always-on extras (small, free):
    extras/schema.sql           CREATE statements from sqlite_master
    extras/cycle_stats.csv      Last 30 days of parser cycle health
    extras/machines.json        Per-machine config (IP, type, tx1_tz_offset_hours)
    extras/o100_live_probe.csv  Live SMB read of each Takeuchi O100.txt at sample time
    extras/sha256_manifest.txt  SHA256 of every copied raw log

Output layout (on USB):
    drill_sample_YYYYMMDD_HHMMSS/
        drill_monitor_snapshot.db        (hot-copied via sqlite3 .backup)
        extras/
            schema.sql
            cycle_stats.csv
            machines.json
            o100_live_probe.csv
            sha256_manifest.txt
        app_log/
            drill_monitor.log            (live + rotated backups)
        o100_live/
            M01/O100.txt                 (SMB read at sample time, ground truth)
            ...
        M01/
            20260508/                    (date subfolder, matches SMB layout)
                08Drive.Log
                08TX1.Log
                08FILE.Log
            20260507/
                07TX1.Log                (TX1+FILE only when --audit goes back further than Drive)
                07FILE.Log
            ...
        L1/
            20260508/20260508_ClsPLCTrd.log
            programs/                    (ProcTimeStart.txt, ProcTimeEnd.txt, ...)
        ...
        MANIFEST.txt                     (summary)
"""

import argparse
import csv
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import string
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Live SMB read of NcProgram\O100.txt — defensive cap (file is normally <2KB).
LIVE_O100_MAX_BYTES = 64 * 1024
LIVE_O100_TEMPLATE = r"\\{ip}\NcProgram\O100.txt"


def _import_o100_parser():
    """Best-effort import of parsers.o100_parser. Returns parse fn or None.

    usb_sample is otherwise stdlib-only; we only pull in the project parser
    when needed for the live O100 probe so the script still runs on a stale
    deploy without the Phase 3 code.
    """
    try:
        if PROJECT_ROOT not in sys.path:
            sys.path.insert(0, PROJECT_ROOT)
        from parsers.o100_parser import parse_o100_content
        return parse_o100_content
    except Exception as e:
        print("WARN: o100_parser import failed ({}); raw will still be saved".format(e))
        return None


def load_settings():
    """Load settings, honoring DRILL_DEV_CONFIG override (matches base_parser)."""
    path = os.path.join(PROJECT_ROOT, "config", "settings.json")
    env_override = os.environ.get("DRILL_DEV_CONFIG")
    if env_override:
        override_path = env_override if os.path.isabs(env_override) else os.path.join(PROJECT_ROOT, env_override)
        if os.path.exists(override_path):
            path = override_path
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_machines():
    path = os.path.join(PROJECT_ROOT, "config", "machines.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [m for m in data["machines"] if m.get("enabled")]


def auto_detect_usb():
    """Return the first removable drive that looks like USB.

    Heuristic: any writable letter C:..Z: other than C: and the OS drive.
    Prefer empty ones; otherwise first writable.
    """
    system_drive = os.environ.get("SystemDrive", "C:").rstrip("\\")
    candidates = []
    for letter in string.ascii_uppercase:
        if letter + ":" == system_drive:
            continue
        root = letter + ":\\"
        if os.path.exists(root):
            try:
                _ = os.listdir(root)
                candidates.append(root)
            except OSError:
                pass
    return candidates[0] if candidates else None


def resolve_target(arg):
    if arg:
        # On Windows, a bare "D:" resolves to CWD on D:, not the root, so
        # append a separator.  os.path.normpath handles mixed slashes.
        target_root = arg
        if len(target_root) == 2 and target_root[1] == ":":
            target_root = target_root + os.sep
    else:
        detected = auto_detect_usb()
        if not detected:
            print("ERROR: no USB drive found. Pass a drive letter as arg, e.g.:")
            print("    python tools/usb_sample.py D:")
            sys.exit(1)
        target_root = detected
        print("Auto-detected USB: {}".format(target_root))

    target_root = os.path.normpath(target_root)
    if not os.path.exists(target_root):
        print("ERROR: path not found: {}".format(target_root))
        sys.exit(1)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(target_root, "drill_sample_" + stamp)
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def compute_sha256(path):
    """Stream-read SHA256 for any file size."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _record_copied(dst, out_dir, hashes, manifest_lines, label):
    """Hash dst, append to hashes list and manifest lines. Returns size_bytes."""
    sha = compute_sha256(dst)
    rel = os.path.relpath(dst, out_dir).replace("\\", "/")
    hashes.append((rel, sha))
    size = os.path.getsize(dst)
    if size >= 1024 * 1024:
        size_str = "{:.1f} MB".format(size / (1024 * 1024))
    else:
        size_str = "{} KB".format(size // 1024)
    manifest_lines.append("  {} ({}, sha256={}...)".format(label, size_str, sha[:12]))
    return size


def copy_takeuchi_logs(backup_root, machines, out_dir, manifest, hashes,
                       drive_days, tx1_days, file_days):
    """Copy Drive.Log + TX1.Log + FILE.Log per Takeuchi machine.

    Each file type has its own day window, useful for audits where TX1 / FILE
    need a wider history (LoadProgram events, O100.txt content) but Drive.Log
    is bulky and only used for sample cross-check.

    Output layout: {out_dir}/{machine_id}/{YYYYMMDD}/{DD}{suffix}
    """
    today = datetime.date.today()
    fname_specs = [
        ("Drive.Log", drive_days),
        ("TX1.Log", tx1_days),
        ("FILE.Log", file_days),
    ]
    max_days = max(drive_days, tx1_days, file_days)
    min_days = min(drive_days, tx1_days, file_days)

    manifest.append(
        "Today: {}  windows: drive={}d tx1={}d file={}d".format(
            today.strftime("%Y%m%d"), drive_days, tx1_days, file_days))
    manifest.append("")
    manifest.append("=== Takeuchi Drive.Log + TX1.Log + FILE.Log ===")

    copied = 0
    missing = []
    for m in machines:
        if m.get("type") != "takeuchi":
            continue
        mid = m["id"]
        any_copied = False

        for i in range(max_days):
            d = today - datetime.timedelta(days=i)
            date_dir = d.strftime("%Y%m%d")
            dd = d.strftime("%d")
            src_date_dir = os.path.join(backup_root, mid, date_dir)

            if not os.path.isdir(src_date_dir):
                if i < min_days:
                    missing.append("{}: no folder {}".format(mid, src_date_dir))
                continue

            dst_date_dir = os.path.join(out_dir, mid, date_dir)
            for suffix, days in fname_specs:
                if i >= days:
                    continue
                fname = "{}{}".format(dd, suffix)
                src = os.path.join(src_date_dir, fname)
                if os.path.isfile(src):
                    if not any_copied:
                        any_copied = True
                    os.makedirs(dst_date_dir, exist_ok=True)
                    dst = os.path.join(dst_date_dir, fname)
                    shutil.copy2(src, dst)
                    _record_copied(dst, out_dir, hashes, manifest,
                                   "{}/{}/{}".format(mid, date_dir, fname))
                    copied += 1
                elif i < days:
                    manifest.append("  {}/{}/{} MISSING".format(mid, date_dir, fname))
        print("  {}: done".format(mid))

    return copied, missing


def copy_kataoka_logs(backup_root, machines, out_dir, manifest, hashes,
                      kataoka_days):
    """Copy ClsPLCTrd daily logs + programs/ dir per Kataoka laser."""
    today = datetime.date.today()

    manifest.append("")
    manifest.append("=== Kataoka ClsPLCTrd + programs/ ===")
    manifest.append("Window: {}d".format(kataoka_days))

    copied = 0
    for m in machines:
        if m.get("type") != "kataoka":
            continue
        mid = m["id"]
        any_copied = False

        for i in range(kataoka_days):
            d = today - datetime.timedelta(days=i)
            date_str = d.strftime("%Y%m%d")
            src_dir = os.path.join(backup_root, mid, date_str)
            if not os.path.isdir(src_dir):
                manifest.append("  {}/{} MISSING (no folder)".format(mid, date_str))
                continue
            fname = "{}_ClsPLCTrd.log".format(date_str)
            src = os.path.join(src_dir, fname)
            if os.path.isfile(src):
                any_copied = True
                dst_subdir = os.path.join(out_dir, mid, date_str)
                os.makedirs(dst_subdir, exist_ok=True)
                dst = os.path.join(dst_subdir, fname)
                shutil.copy2(src, dst)
                _record_copied(dst, out_dir, hashes, manifest,
                               "{}/{}/{}".format(mid, date_str, fname))
                copied += 1
            else:
                manifest.append("  {}/{}/{} MISSING".format(mid, date_str, fname))

        # Copy programs/ dir (work order files: ProcTimeStart.txt, ProcTimeEnd.txt, ...)
        # L1 currently has no INFO share so this dir won't exist.
        programs_src = os.path.join(backup_root, mid, "programs")
        if os.path.isdir(programs_src):
            any_copied = True
            programs_dst = os.path.join(out_dir, mid, "programs")
            try:
                shutil.copytree(programs_src, programs_dst, dirs_exist_ok=True)
                n_files = 0
                for root, _, files in os.walk(programs_dst):
                    for fn in files:
                        full = os.path.join(root, fn)
                        _record_copied(full, out_dir, hashes, manifest,
                                       os.path.relpath(full, out_dir).replace("\\", "/"))
                        n_files += 1
                manifest.append("  {}/programs/ ({} files total)".format(mid, n_files))
            except Exception as e:
                manifest.append("  {}/programs/ FAILED: {}".format(mid, e))
        else:
            manifest.append("  {}/programs/ MISSING (skip_info?)".format(mid))
        if any_copied:
            print("  {}: done".format(mid))
        else:
            print("  {}: nothing to copy".format(mid))

    return copied


def probe_live_o100(machines, out_dir, manifest, hashes):
    """Read \\\\{ip}\\NcProgram\\O100.txt live from each Takeuchi machine.

    Provides ground-truth tap at sample time so reviewer can cross-check
    against o100_snapshots and machine_current_state.current_o100_subs in
    the DB snapshot. Saves raw bytes plus a CSV summary.

    No-op on non-Windows (SMB UNC paths can't resolve outside Windows).
    Per-machine errors are captured in the CSV; the loop never aborts.
    """
    manifest.append("")
    manifest.append("=== Live O100.txt SMB probe ===")
    if os.name != "nt":
        manifest.append("  SKIPPED (non-Windows; SMB unavailable)")
        print("Live O100 probe: skipped (non-Windows)")
        return 0

    parse_fn = _import_o100_parser()
    extras_dir = os.path.join(out_dir, "extras")
    os.makedirs(extras_dir, exist_ok=True)
    csv_path = os.path.join(extras_dir, "o100_live_probe.csv")
    live_root = os.path.join(out_dir, "o100_live")

    rows = []
    n_ok = 0
    n_takeuchi = 0
    for m in machines:
        if m.get("type") != "takeuchi":
            continue
        n_takeuchi += 1
        mid = m["id"]
        ip = m.get("ip", "")
        probed_at = datetime.datetime.now().isoformat()

        if not ip:
            rows.append([mid, ip, probed_at, "", "", "", "", "no_ip", "machines.json missing ip"])
            manifest.append("  {} SKIP no ip in machines.json".format(mid))
            continue

        path = LIVE_O100_TEMPLATE.format(ip=ip)
        try:
            st = os.stat(path)
            size = st.st_size
            mtime_iso = datetime.datetime.fromtimestamp(st.st_mtime).isoformat()
            if size > LIVE_O100_MAX_BYTES:
                rows.append([mid, ip, probed_at, mtime_iso, str(size), "", "", "too_large", ""])
                manifest.append("  {} SKIP size={} > {} bytes".format(mid, size, LIVE_O100_MAX_BYTES))
                continue
            with open(path, "rb") as f:
                raw = f.read()
            sha = hashlib.sha256(raw).hexdigest()

            mid_dir = os.path.join(live_root, mid)
            os.makedirs(mid_dir, exist_ok=True)
            dst = os.path.join(mid_dir, "O100.txt")
            with open(dst, "wb") as fw:
                fw.write(raw)
            rel = os.path.relpath(dst, out_dir).replace("\\", "/")
            hashes.append((rel, sha))

            active_subs = ""
            parse_status = "no_parser"
            if parse_fn is not None:
                try:
                    parsed = parse_fn(raw.decode("cp932", errors="replace"))
                    active_subs = json.dumps(parsed.get("active_subs", []))
                    parse_status = "ok"
                except Exception as e:
                    parse_status = "parse_fail:{}".format(type(e).__name__)

            rows.append([mid, ip, probed_at, mtime_iso, str(size), sha[:16],
                         active_subs, parse_status, ""])
            manifest.append("  {} OK size={}B mtime={} active_subs={}".format(
                mid, size, mtime_iso, active_subs or "(unparsed)"))
            n_ok += 1
        except OSError as e:
            rows.append([mid, ip, probed_at, "", "", "", "", "smb_error",
                         "{}: {}".format(type(e).__name__, e)])
            manifest.append("  {} SMB error: {}".format(mid, e))
        except Exception as e:
            rows.append([mid, ip, probed_at, "", "", "", "", "fail",
                         "{}: {}".format(type(e).__name__, e)])
            manifest.append("  {} unexpected error: {}".format(mid, e))

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["machine_id", "ip", "probed_at_local", "smb_mtime",
                    "smb_size", "sha256_short", "active_subs", "status", "error"])
        w.writerows(rows)
    print("Live O100 probe: {}/{} OK".format(n_ok, n_takeuchi))
    return n_ok


def copy_app_log(out_dir, manifest, hashes):
    """Copy drill_monitor.log + rotation backups (drill_monitor.log.1..5)."""
    manifest.append("")
    manifest.append("=== Application log ===")
    log_dir = os.path.join(out_dir, "app_log")
    os.makedirs(log_dir, exist_ok=True)
    copied = 0
    candidates = [f for f in os.listdir(PROJECT_ROOT) if f.startswith("drill_monitor.log")]
    for fname in sorted(candidates):
        src = os.path.join(PROJECT_ROOT, fname)
        dst = os.path.join(log_dir, fname)
        try:
            # The live log may be open by the running monitor; read+write
            # avoids needing exclusive access that copy2 would request.
            with open(src, "rb") as fr, open(dst, "wb") as fw:
                fw.write(fr.read())
            _record_copied(dst, out_dir, hashes, manifest, "app_log/" + fname)
            copied += 1
        except Exception as e:
            manifest.append("  app_log/{} FAILED: {}".format(fname, e))
    print("App log: {} files copied".format(copied))
    return copied


def snapshot_db(db_path, out_dir, manifest):
    if not os.path.isfile(db_path):
        print("WARN: DB not found at {}, skipping snapshot".format(db_path))
        manifest.append("DB snapshot: SKIPPED (source not found)")
        return None

    dst = os.path.join(out_dir, "drill_monitor_snapshot.db")
    src_conn = sqlite3.connect(db_path)
    dst_conn = sqlite3.connect(dst)
    try:
        src_conn.backup(dst_conn)
    finally:
        src_conn.close()
        dst_conn.close()
    size_mb = os.path.getsize(dst) / (1024 * 1024)
    print("DB snapshot: {:.1f} MB".format(size_mb))
    manifest.append("")
    manifest.append("DB snapshot: {:.1f} MB".format(size_mb))
    return dst


def export_extras(snapshot_db_path, out_dir, manifest, hashes):
    """Always-on small exports: schema.sql + cycle_stats.csv.

    Run against the snapshot copy (not the live DB) to avoid holding any
    locks on the production file beyond the .backup() call.
    """
    if not snapshot_db_path or not os.path.isfile(snapshot_db_path):
        return

    extras_dir = os.path.join(out_dir, "extras")
    os.makedirs(extras_dir, exist_ok=True)
    manifest.append("")
    manifest.append("=== Extras ===")

    # machines.json — needed for per-machine TZ + IP verification on dev side.
    src_machines = os.path.join(PROJECT_ROOT, "config", "machines.json")
    if os.path.isfile(src_machines):
        dst_machines = os.path.join(extras_dir, "machines.json")
        shutil.copy2(src_machines, dst_machines)
        _record_copied(dst_machines, out_dir, hashes, manifest, "extras/machines.json")
    else:
        manifest.append("  extras/machines.json MISSING (source not found)")

    conn = sqlite3.connect(snapshot_db_path)
    try:
        # schema.sql
        schema_path = os.path.join(extras_dir, "schema.sql")
        rows = conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL ORDER BY type, name"
        ).fetchall()
        with open(schema_path, "w", encoding="utf-8") as f:
            for t, n, s in rows:
                f.write("-- {} {}\n{};\n\n".format(t, n, s))
        manifest.append("  extras/schema.sql ({} objects)".format(len(rows)))

        # cycle_stats.csv (last 30 days; small even when daily)
        cs_path = os.path.join(extras_dir, "cycle_stats.csv")
        try:
            cur = conn.execute("""
                SELECT cycle_start, cycle_end, took_ms, interval_secs,
                       steps_ok, steps_failed, failed_step_names
                FROM cycle_stats
                WHERE cycle_start >= datetime('now', '-30 days')
                ORDER BY cycle_start DESC
            """)
            rows = cur.fetchall()
            with open(cs_path, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["cycle_start", "cycle_end", "took_ms", "interval_secs",
                            "steps_ok", "steps_failed", "failed_step_names"])
                w.writerows(rows)
            manifest.append("  extras/cycle_stats.csv ({} rows)".format(len(rows)))
        except sqlite3.OperationalError as e:
            manifest.append("  extras/cycle_stats.csv SKIPPED: {}".format(e))
    finally:
        conn.close()


def write_sha256_manifest(out_dir, hashes):
    """Dump (relpath, sha256) pairs into extras/sha256_manifest.txt."""
    extras_dir = os.path.join(out_dir, "extras")
    os.makedirs(extras_dir, exist_ok=True)
    path = os.path.join(extras_dir, "sha256_manifest.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# SHA256 of every raw file copied (relative to drill_sample root)\n")
        for rel, sha in sorted(hashes):
            f.write("{}  {}\n".format(sha, rel))


def parse_args():
    p = argparse.ArgumentParser(
        description="USB-bound drill-monitor sample collector",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("target", nargs="?",
                   help="USB drive letter or path (auto-detect if omitted)")
    p.add_argument("--audit", action="store_true",
                   help="Wide window for full audit (TX1=8d, FILE=8d)")
    p.add_argument("--drive-days", type=int, default=None)
    p.add_argument("--tx1-days", type=int, default=None)
    p.add_argument("--file-days", type=int, default=None)
    p.add_argument("--kataoka-days", type=int, default=None)
    return p.parse_args()


def main():
    args = parse_args()

    if args.audit:
        defaults = dict(drive=2, tx1=8, file=8, kataoka=2)
    else:
        defaults = dict(drive=2, tx1=2, file=2, kataoka=2)

    drive_days = args.drive_days if args.drive_days is not None else defaults["drive"]
    tx1_days = args.tx1_days if args.tx1_days is not None else defaults["tx1"]
    file_days = args.file_days if args.file_days is not None else defaults["file"]
    kataoka_days = args.kataoka_days if args.kataoka_days is not None else defaults["kataoka"]

    out_dir = resolve_target(args.target)
    print("Output: {}".format(out_dir))
    print("Windows: drive={}d tx1={}d file={}d kataoka={}d".format(
        drive_days, tx1_days, file_days, kataoka_days))

    settings = load_settings()
    backup_root = settings.get("backup_root", "C:\\DrillLogs")
    db_path = settings.get("db_path", "drill_monitor.db")
    if not os.path.isabs(db_path):
        db_path = os.path.join(PROJECT_ROOT, db_path)

    machines = load_machines()
    print("Machines: {} enabled".format(len(machines)))

    n_takeuchi = sum(1 for m in machines if m.get("type") == "takeuchi")
    n_kataoka = sum(1 for m in machines if m.get("type") == "kataoka")
    manifest = [
        "Drill Sample Manifest",
        "=====================",
        "Generated: {}".format(datetime.datetime.now().isoformat()),
        "Mode: {}".format("AUDIT" if args.audit else "DEFAULT"),
        "Source backup_root: {}".format(backup_root),
        "Source DB: {}".format(db_path),
        "Machines: {} enabled (takeuchi={}, kataoka={})".format(
            len(machines), n_takeuchi, n_kataoka),
        "",
    ]
    hashes = []

    print("Copying Takeuchi Drive.Log + TX1.Log + FILE.Log...")
    n_takeuchi_files, missing = copy_takeuchi_logs(
        backup_root, machines, out_dir, manifest, hashes,
        drive_days=drive_days, tx1_days=tx1_days, file_days=file_days)

    print("Copying Kataoka ClsPLCTrd + programs/...")
    n_kataoka_files = copy_kataoka_logs(
        backup_root, machines, out_dir, manifest, hashes,
        kataoka_days=kataoka_days)

    if missing:
        manifest.append("")
        manifest.append("=== Missing Takeuchi folders ===")
        manifest.extend(missing)

    print("Copying app log...")
    n_app_log = copy_app_log(out_dir, manifest, hashes)

    print("Probing live O100.txt over SMB...")
    n_o100_live = probe_live_o100(machines, out_dir, manifest, hashes)

    print("Snapshotting DB...")
    snapshot_path = snapshot_db(db_path, out_dir, manifest)

    print("Exporting schema + cycle_stats + machines.json...")
    export_extras(snapshot_path, out_dir, manifest, hashes)

    write_sha256_manifest(out_dir, hashes)

    with open(os.path.join(out_dir, "MANIFEST.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(manifest) + "\n")

    print("")
    print("Done.")
    print("  Takeuchi files: {}".format(n_takeuchi_files))
    print("  Kataoka files:  {}".format(n_kataoka_files))
    print("  App log files:  {}".format(n_app_log))
    print("  Live O100 OK:   {}".format(n_o100_live))
    print("  SHA256 entries: {}".format(len(hashes)))
    if missing:
        print("  Missing Takeuchi folders: {}".format(len(missing)))
    print("Output: {}".format(out_dir))


if __name__ == "__main__":
    main()

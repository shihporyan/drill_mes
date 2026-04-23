"""Copy today + yesterday Drive.Log files and a DB snapshot to USB.

Run on the production compute PC. Collects enough data off-site so hole
count and hourly_utilization can be cross-verified against raw Drive.Log
on the dev machine.

Usage:
    python tools/usb_sample.py              # Auto-detect USB drive
    python tools/usb_sample.py D:           # Explicit drive letter
    python tools/usb_sample.py D:\subdir    # Explicit target path

Output layout (on USB):
    drill_sample_YYYYMMDD_HHMMSS/
        drill_monitor_snapshot.db    (hot-copied via sqlite3 .backup)
        M01/23Drive.Log              (today)
        M01/22Drive.Log              (yesterday)
        M02/...
        ...
        MANIFEST.txt                 (summary)
"""

import datetime
import json
import os
import shutil
import sqlite3
import string
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_settings():
    path = os.path.join(PROJECT_ROOT, "config", "settings.json")
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


def copy_drive_logs(backup_root, machines, out_dir, manifest):
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    today_dir = today.strftime("%Y%m%d")
    dd_today = today.strftime("%d")
    dd_yesterday = yesterday.strftime("%d")

    manifest.append("Today day_prefix: {}  Yesterday: {}  Date dir: {}"
                    .format(dd_today, dd_yesterday, today_dir))
    manifest.append("")

    copied = 0
    missing = []
    for m in machines:
        mid = m["id"]
        src_dir = os.path.join(backup_root, mid, today_dir)
        if not os.path.isdir(src_dir):
            missing.append("{}: no folder {}".format(mid, src_dir))
            continue

        dst_machine = os.path.join(out_dir, mid)
        os.makedirs(dst_machine, exist_ok=True)

        for dd in (dd_today, dd_yesterday):
            fname = "{}Drive.Log".format(dd)
            src = os.path.join(src_dir, fname)
            if os.path.isfile(src):
                dst = os.path.join(dst_machine, fname)
                shutil.copy2(src, dst)
                size_kb = os.path.getsize(dst) // 1024
                manifest.append("  {}/{} ({} KB)".format(mid, fname, size_kb))
                copied += 1
            else:
                manifest.append("  {}/{} MISSING".format(mid, fname))
        print("  {}: done".format(mid))

    return copied, missing


def snapshot_db(db_path, out_dir, manifest):
    if not os.path.isfile(db_path):
        print("WARN: DB not found at {}, skipping snapshot".format(db_path))
        manifest.append("DB snapshot: SKIPPED (source not found)")
        return

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


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    out_dir = resolve_target(arg)
    print("Output: {}".format(out_dir))

    settings = load_settings()
    backup_root = settings.get("backup_root", "C:\\DrillLogs")
    db_path = settings.get("db_path", "drill_monitor.db")
    if not os.path.isabs(db_path):
        db_path = os.path.join(PROJECT_ROOT, db_path)

    machines = load_machines()
    print("Machines: {} enabled".format(len(machines)))

    manifest = [
        "Drill Sample Manifest",
        "=====================",
        "Generated: {}".format(datetime.datetime.now().isoformat()),
        "Source backup_root: {}".format(backup_root),
        "Source DB: {}".format(db_path),
        "Machines: {} enabled".format(len(machines)),
        "",
        "=== Drive.Log files ===",
    ]

    copied, missing = copy_drive_logs(backup_root, machines, out_dir, manifest)

    if missing:
        manifest.append("")
        manifest.append("=== Missing folders ===")
        manifest.extend(missing)

    snapshot_db(db_path, out_dir, manifest)

    with open(os.path.join(out_dir, "MANIFEST.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(manifest) + "\n")

    print("")
    print("Done. Copied {} Drive.Log files.".format(copied))
    if missing:
        print("  ({} machine folders missing)".format(len(missing)))
    print("Output: {}".format(out_dir))


if __name__ == "__main__":
    main()

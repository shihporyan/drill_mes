"""Copy raw machine logs + DB snapshot + app log to USB for off-site verification.

Run on the production compute PC. Collects enough data so hole count,
hourly_utilization, work-order assignment, and laser shift averages can
all be cross-verified against raw logs on the dev machine.

Usage:
    python tools/usb_sample.py              # Auto-detect USB drive
    python tools/usb_sample.py D:           # Explicit drive letter
    python tools/usb_sample.py D:\\subdir   # Explicit target path

Output layout (on USB):
    drill_sample_YYYYMMDD_HHMMSS/
        drill_monitor_snapshot.db        (hot-copied via sqlite3 .backup)
        app_log/
            drill_monitor.log            (live + rotated backups)
        M01/                             (Takeuchi: today + yesterday)
            27Drive.Log / 27TX1.Log
            28Drive.Log / 28TX1.Log
        ...
        L1/                              (Kataoka: today + yesterday)
            20260427/20260427_ClsPLCTrd.log
            20260428/20260428_ClsPLCTrd.log
            programs/                    (ProcTimeStart.txt, ProcTimeEnd.txt, ...)
        ...
        MANIFEST.txt                     (summary)
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


def copy_takeuchi_logs(backup_root, machines, out_dir, manifest):
    """Copy Drive.Log + TX1.Log for today + yesterday for each Takeuchi machine."""
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    today_dir = today.strftime("%Y%m%d")
    yesterday_dir = yesterday.strftime("%Y%m%d")
    dd_today = today.strftime("%d")
    dd_yesterday = yesterday.strftime("%d")

    manifest.append("Today day_prefix: {}  Yesterday: {}".format(dd_today, dd_yesterday))
    manifest.append("Today date dir: {}  Yesterday: {}".format(today_dir, yesterday_dir))
    manifest.append("")
    manifest.append("=== Takeuchi Drive.Log + TX1.Log ===")

    copied = 0
    missing = []
    for m in machines:
        if m.get("type") != "takeuchi":
            continue
        mid = m["id"]
        dst_machine = os.path.join(out_dir, mid)
        any_copied = False

        for date_dir, dd in ((today_dir, dd_today), (yesterday_dir, dd_yesterday)):
            src_dir = os.path.join(backup_root, mid, date_dir)
            if not os.path.isdir(src_dir):
                missing.append("{}: no folder {}".format(mid, src_dir))
                continue
            for fname in ("{}Drive.Log".format(dd), "{}TX1.Log".format(dd)):
                src = os.path.join(src_dir, fname)
                if os.path.isfile(src):
                    if not any_copied:
                        os.makedirs(dst_machine, exist_ok=True)
                        any_copied = True
                    dst = os.path.join(dst_machine, fname)
                    shutil.copy2(src, dst)
                    size_kb = os.path.getsize(dst) // 1024
                    manifest.append("  {}/{} ({} KB)".format(mid, fname, size_kb))
                    copied += 1
                else:
                    manifest.append("  {}/{} MISSING".format(mid, fname))
        print("  {}: done".format(mid))

    return copied, missing


def copy_kataoka_logs(backup_root, machines, out_dir, manifest):
    """Copy ClsPLCTrd daily logs + programs/ dir for each Kataoka laser."""
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    today_str = today.strftime("%Y%m%d")
    yesterday_str = yesterday.strftime("%Y%m%d")

    manifest.append("")
    manifest.append("=== Kataoka ClsPLCTrd + programs/ ===")

    copied = 0
    for m in machines:
        if m.get("type") != "kataoka":
            continue
        mid = m["id"]
        dst_machine = os.path.join(out_dir, mid)
        any_copied = False

        for date_str in (today_str, yesterday_str):
            src_dir = os.path.join(backup_root, mid, date_str)
            if not os.path.isdir(src_dir):
                manifest.append("  {}/{} MISSING (no folder)".format(mid, date_str))
                continue
            fname = "{}_ClsPLCTrd.log".format(date_str)
            src = os.path.join(src_dir, fname)
            if os.path.isfile(src):
                if not any_copied:
                    os.makedirs(dst_machine, exist_ok=True)
                    any_copied = True
                dst_subdir = os.path.join(dst_machine, date_str)
                os.makedirs(dst_subdir, exist_ok=True)
                dst = os.path.join(dst_subdir, fname)
                shutil.copy2(src, dst)
                size_mb = os.path.getsize(dst) / (1024 * 1024)
                manifest.append("  {}/{}/{} ({:.1f} MB)".format(mid, date_str, fname, size_mb))
                copied += 1
            else:
                manifest.append("  {}/{}/{} MISSING".format(mid, date_str, fname))

        # Copy programs/ dir (work order files: ProcTimeStart.txt, ProcTimeEnd.txt, ...)
        # L1 currently has no INFO share so this dir won't exist.
        programs_src = os.path.join(backup_root, mid, "programs")
        if os.path.isdir(programs_src):
            if not any_copied:
                os.makedirs(dst_machine, exist_ok=True)
                any_copied = True
            programs_dst = os.path.join(dst_machine, "programs")
            try:
                shutil.copytree(programs_src, programs_dst, dirs_exist_ok=True)
                n_files = sum(len(files) for _, _, files in os.walk(programs_dst))
                manifest.append("  {}/programs/ ({} files)".format(mid, n_files))
            except Exception as e:
                manifest.append("  {}/programs/ FAILED: {}".format(mid, e))
        else:
            manifest.append("  {}/programs/ MISSING (skip_info?)".format(mid))
        print("  {}: done".format(mid))

    return copied


def copy_app_log(out_dir, manifest):
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
            size_kb = os.path.getsize(dst) // 1024
            manifest.append("  app_log/{} ({} KB)".format(fname, size_kb))
            copied += 1
        except Exception as e:
            manifest.append("  app_log/{} FAILED: {}".format(fname, e))
    print("App log: {} files copied".format(copied))
    return copied


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

    n_takeuchi = sum(1 for m in machines if m.get("type") == "takeuchi")
    n_kataoka = sum(1 for m in machines if m.get("type") == "kataoka")
    manifest = [
        "Drill Sample Manifest",
        "=====================",
        "Generated: {}".format(datetime.datetime.now().isoformat()),
        "Source backup_root: {}".format(backup_root),
        "Source DB: {}".format(db_path),
        "Machines: {} enabled (takeuchi={}, kataoka={})".format(
            len(machines), n_takeuchi, n_kataoka),
        "",
    ]

    print("Copying Takeuchi Drive.Log + TX1.Log...")
    n_takeuchi_files, missing = copy_takeuchi_logs(backup_root, machines, out_dir, manifest)

    print("Copying Kataoka ClsPLCTrd + programs/...")
    n_kataoka_files = copy_kataoka_logs(backup_root, machines, out_dir, manifest)

    if missing:
        manifest.append("")
        manifest.append("=== Missing Takeuchi folders ===")
        manifest.extend(missing)

    print("Copying app log...")
    n_app_log = copy_app_log(out_dir, manifest)

    print("Snapshotting DB...")
    snapshot_db(db_path, out_dir, manifest)

    with open(os.path.join(out_dir, "MANIFEST.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(manifest) + "\n")

    print("")
    print("Done.")
    print("  Takeuchi files: {}".format(n_takeuchi_files))
    print("  Kataoka files:  {}".format(n_kataoka_files))
    print("  App log files:  {}".format(n_app_log))
    if missing:
        print("  Missing Takeuchi folders: {}".format(len(missing)))
    print("Output: {}".format(out_dir))


if __name__ == "__main__":
    main()

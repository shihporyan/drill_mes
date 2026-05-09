"""USB-bound probe of production state — parser version + config + ClsPLCTrd inventory.

Run on the production compute PC; outputs a folder ready to USB-back to dev for
cross-check. Stdlib-only (no sqlite3 CLI, no extra deps).

Captures three things needed for the 5/9 Step B planning:

1. Parser code version
   - SHA256 of every .py under parsers/ + collector/
   - Head 30 lines of laser_log_parser.py to spot-check the LSR-removal commit
     (1d91a79: BEAM_HOLE_PATTERN should be present)
   - git_head.txt with the current HEAD commit hash + last 10 commits

2. Config snapshot
   - Full copy of config/*.json (settings.json + machines.json)
   - Effective backup_root + db_path resolved from settings

3. Laser raw-log inventory
   - For each enabled Kataoka machine, walk {backup_root}/{mid}/ and list
     YYYYMMDD subfolders that contain {date}_ClsPLCTrd.log
   - Per file: size + first/last beam-OK event timestamp + total event count
     (so we know which days are backfill-able and which were PLC-down)

Usage:
    python tools\\probe_production_state.py              # auto-detect USB
    python tools\\probe_production_state.py D:           # explicit drive
    python tools\\probe_production_state.py /tmp/test    # dev smoke test

Output layout (on USB):
    prod_state_YYYYMMDD_HHMMSS/
        REPORT.txt                   # human-readable summary
        parsers_sha256.txt
        config/
            settings.json
            machines.json
        git_head.txt
        laser_inventory.txt          # per-machine per-date file size + beam count
"""

import datetime
import hashlib
import json
import os
import re
import shutil
import string
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# Reuse usb_sample's auto_detect helper pattern; inline here to keep this
# script standalone (the operator can run it without us deploying anything else).


def auto_detect_usb():
    system_drive = os.environ.get("SystemDrive", "C:").rstrip("\\")
    for letter in string.ascii_uppercase:
        if letter + ":" == system_drive:
            continue
        root = letter + ":\\"
        if os.path.exists(root):
            try:
                _ = os.listdir(root)
                return root
            except OSError:
                continue
    return None


def resolve_target(arg):
    if arg:
        target = arg
        if len(target) == 2 and target[1] == ":":
            target = target + os.sep
    else:
        target = auto_detect_usb()
        if not target:
            print("ERROR: no USB; pass a drive letter or path")
            sys.exit(1)
        print("Auto-detected USB: {}".format(target))
    target = os.path.normpath(target)
    if not os.path.exists(target):
        print("ERROR: path not found: {}".format(target))
        sys.exit(1)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(target, "prod_state_" + stamp)
    os.makedirs(out, exist_ok=True)
    return out


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_python_tree(out_dir, lines):
    """SHA256 every .py under parsers/, collector/, tools/, db/, server/."""
    targets = ["parsers", "collector", "tools", "db", "server"]
    rows = []
    for sub in targets:
        full = os.path.join(PROJECT_ROOT, sub)
        if not os.path.isdir(full):
            continue
        for root, _, files in os.walk(full):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(root, fn)
                rel = os.path.relpath(p, PROJECT_ROOT).replace("\\", "/")
                size = os.path.getsize(p)
                sha = sha256_file(p)
                rows.append((rel, size, sha))
    rows.sort()
    path = os.path.join(out_dir, "parsers_sha256.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# {:60s}  {:>10}  sha256\n".format("relpath", "size"))
        for rel, size, sha in rows:
            f.write("{:60s}  {:>10}  {}\n".format(rel, size, sha))
    lines.append("Parsers/collector code: {} .py files hashed -> parsers_sha256.txt".format(len(rows)))


def head_laser_parser(out_dir, lines):
    """Capture identity markers of the laser parser (BEAM_HOLE_PATTERN presence)."""
    src = os.path.join(PROJECT_ROOT, "parsers", "laser_log_parser.py")
    if not os.path.isfile(src):
        lines.append("WARN: parsers/laser_log_parser.py missing")
        return
    with open(src, "r", encoding="utf-8") as f:
        text = f.read()
    has_beam = "BEAM_HOLE_PATTERN" in text and "本加工データ取得" in text
    has_lsr = "extract_hole_count_from_lsr" in text or "LSR_COUNT_PATTERN" in text
    has_load_beam = "load_beam_events_by_station" in text
    lines.append("laser_log_parser identity:")
    lines.append("  BEAM_HOLE_PATTERN present:    {}  (expected: True after 1d91a79)".format(has_beam))
    lines.append("  load_beam_events_by_station:  {}  (expected: True after 1d91a79)".format(has_load_beam))
    lines.append("  legacy LSR helpers present:   {}  (expected: False after 1d91a79)".format(has_lsr))


def copy_config(out_dir, lines):
    src = os.path.join(PROJECT_ROOT, "config")
    dst = os.path.join(out_dir, "config")
    os.makedirs(dst, exist_ok=True)
    n = 0
    for fn in sorted(os.listdir(src)):
        if not fn.endswith(".json"):
            continue
        s = os.path.join(src, fn)
        d = os.path.join(dst, fn)
        shutil.copy2(s, d)
        sha = sha256_file(d)
        lines.append("  config/{} ({} bytes, sha256={}...)".format(fn, os.path.getsize(d), sha[:12]))
        n += 1
    lines.append("Config files copied: {}".format(n))


def git_head(out_dir, lines):
    path = os.path.join(out_dir, "git_head.txt")
    try:
        head = subprocess.check_output(
            ["git", "-C", PROJECT_ROOT, "rev-parse", "HEAD"],
            stderr=subprocess.STDOUT, text=True).strip()
        log = subprocess.check_output(
            ["git", "-C", PROJECT_ROOT, "log", "--oneline", "-20"],
            stderr=subprocess.STDOUT, text=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("HEAD: {}\n\nLast 20 commits:\n{}\n".format(head, log))
        lines.append("Git HEAD: {}".format(head[:12]))
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        with open(path, "w", encoding="utf-8") as f:
            f.write("git unavailable or not a repo: {}\n".format(e))
        lines.append("Git HEAD: UNAVAILABLE ({})".format(type(e).__name__))


def load_settings():
    """Load settings — honors DRILL_DEV_CONFIG."""
    path = os.path.join(PROJECT_ROOT, "config", "settings.json")
    env_override = os.environ.get("DRILL_DEV_CONFIG")
    if env_override:
        override = env_override if os.path.isabs(env_override) else os.path.join(PROJECT_ROOT, env_override)
        if os.path.exists(override):
            path = override
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_machines():
    with open(os.path.join(PROJECT_ROOT, "config", "machines.json"), "r", encoding="utf-8") as f:
        return [m for m in json.load(f)["machines"] if m.get("enabled")]


# ClsPLCTrd is UTF-8 with BOM in production (verified 5/9 sample).
BEAM_PATTERN = re.compile(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}).*本加工データ取得.*加工基盤番号:(\d+)")


def scan_clsplctrd(filepath):
    """Return (size, beam_count, first_ts, last_ts) or (size, 0, None, None) on miss."""
    size = os.path.getsize(filepath)
    n = 0
    first = None
    last = None
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = BEAM_PATTERN.match(line)
                if m:
                    n += 1
                    if first is None:
                        first = m.group(1)
                    last = m.group(1)
    except OSError:
        return (size, -1, None, None)
    return (size, n, first, last)


def laser_inventory(out_dir, settings, machines, lines, scan_from_iso="2026-04-08"):
    backup_root = settings.get("backup_root", "C:\\DrillLogs")
    inv_path = os.path.join(out_dir, "laser_inventory.txt")
    inv_lines = []
    inv_lines.append("# backup_root: {}".format(backup_root))
    inv_lines.append("# scan window: {} onwards (set lower bound for backfill planning)".format(scan_from_iso))
    inv_lines.append("# columns: machine_id  date  size_bytes  beam_count  first_ts  last_ts")
    inv_lines.append("")

    machine_summary = {}
    for m in machines:
        if m.get("type") != "kataoka":
            continue
        mid = m["id"]
        machine_dir = os.path.join(backup_root, mid)
        if not os.path.isdir(machine_dir):
            inv_lines.append("# {} MISSING: no folder {}".format(mid, machine_dir))
            machine_summary[mid] = (0, 0)
            continue
        n_files = 0
        n_beams = 0
        for entry in sorted(os.listdir(machine_dir)):
            full = os.path.join(machine_dir, entry)
            if not os.path.isdir(full):
                continue
            # YYYYMMDD folder
            if not (len(entry) == 8 and entry.isdigit()):
                continue
            date_iso = "{}-{}-{}".format(entry[:4], entry[4:6], entry[6:])
            if date_iso < scan_from_iso:
                continue
            log_path = os.path.join(full, "{}_ClsPLCTrd.log".format(entry))
            if not os.path.isfile(log_path):
                inv_lines.append("{}  {}  -  MISSING".format(mid, date_iso))
                continue
            size, n, first_ts, last_ts = scan_clsplctrd(log_path)
            inv_lines.append("{}  {}  {:>12}  {:>8}  {}  {}".format(
                mid, date_iso, size, n,
                first_ts or "-", last_ts or "-"))
            n_files += 1
            n_beams += max(n, 0)
        machine_summary[mid] = (n_files, n_beams)

    with open(inv_path, "w", encoding="utf-8") as f:
        f.write("\n".join(inv_lines) + "\n")

    lines.append("Laser ClsPLCTrd inventory ({} onwards):".format(scan_from_iso))
    for mid, (nf, nb) in sorted(machine_summary.items()):
        lines.append("  {}: {} files, {:,} beam-OK events total".format(mid, nf, nb))


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    out_dir = resolve_target(arg)
    print("Output: {}".format(out_dir))

    settings = load_settings()
    machines = load_machines()

    lines = []
    lines.append("Production state probe")
    lines.append("======================")
    lines.append("Generated: {}".format(datetime.datetime.now().isoformat()))
    lines.append("Host: {}".format(os.environ.get("COMPUTERNAME") or os.uname().nodename))
    lines.append("Project root: {}".format(PROJECT_ROOT))
    lines.append("backup_root: {}".format(settings.get("backup_root")))
    lines.append("db_path: {}".format(settings.get("db_path")))
    lines.append("")

    print("[1/4] Hashing Python files...")
    hash_python_tree(out_dir, lines)
    head_laser_parser(out_dir, lines)
    lines.append("")

    print("[2/4] Copying config...")
    copy_config(out_dir, lines)
    lines.append("")

    print("[3/4] Capturing git HEAD...")
    git_head(out_dir, lines)
    lines.append("")

    print("[4/4] Inventorying laser ClsPLCTrd logs (this may scan ~30 days × 4 lasers)...")
    laser_inventory(out_dir, settings, machines, lines)

    report_path = os.path.join(out_dir, "REPORT.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print("")
    print("Done.")
    print("Output: {}".format(out_dir))
    print("Pull entire folder back to dev for analysis.")


if __name__ == "__main__":
    main()

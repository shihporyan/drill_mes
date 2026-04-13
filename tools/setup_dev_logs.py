"""
Set up mock dev_logs directory from original_logs/machine_logs.

Copies *Drive.Log files into the directory structure expected by the parser:
    dev_logs/{machine_id}/{YYYYMMDD}/{DD}Drive.Log

Re-runnable (idempotent). Only copies Drive.Log files.

Usage:
    python tools/setup_dev_logs.py
"""

import os
import re
import shutil

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_DIR = os.path.join(PROJECT_ROOT, "original_logs", "machine_logs")
DEV_LOGS_DIR = os.path.join(PROJECT_ROOT, "dev_logs")


def extract_machine_id(dirname):
    """Extract machine ID from directory name like 'M13-LOG-260407-TIME1906'."""
    match = re.match(r"^(M\d+)-", dirname)
    return match.group(1) if match else None


def get_date_from_log(filepath, day_prefix):
    """Read log file to determine YYYYMMDD date directory.

    Reads lines until finding one where the day matches the file's day_prefix,
    then returns the full YYYYMMDD string.

    Args:
        filepath: Path to the Drive.Log file.
        day_prefix: Two-digit day string from filename (e.g. '07').

    Returns:
        str: Date string like '20260407', or None if can't determine.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # First field is date: 2026/04/07
                date_str = line.split(",")[0].strip()
                match = re.match(r"^(\d{4})/(\d{2})/(\d{2})$", date_str)
                if match:
                    year, month, day = match.groups()
                    if day == day_prefix:
                        return "{}{}{}".format(year, month, day)
        # Fallback: if no matching day found, use first valid date line
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                date_str = line.split(",")[0].strip()
                match = re.match(r"^(\d{4})/(\d{2})/(\d{2})$", date_str)
                if match:
                    year, month, _ = match.groups()
                    return "{}{}{}".format(year, month, day_prefix)
    except Exception as e:
        print("  WARNING: Could not read {}: {}".format(filepath, e))
    return None


def setup_dev_logs():
    """Copy Drive.Log files into mock directory structure."""
    if not os.path.isdir(SOURCE_DIR):
        print("ERROR: Source directory not found: {}".format(SOURCE_DIR))
        return

    print("Source: {}".format(SOURCE_DIR))
    print("Target: {}".format(DEV_LOGS_DIR))
    print()

    copied = 0
    skipped = 0

    for dirname in sorted(os.listdir(SOURCE_DIR)):
        src_path = os.path.join(SOURCE_DIR, dirname)
        if not os.path.isdir(src_path):
            continue

        machine_id = extract_machine_id(dirname)
        if not machine_id:
            print("SKIP: Cannot extract machine ID from '{}'".format(dirname))
            continue

        print("[{}] Scanning {}".format(machine_id, dirname))

        for filename in sorted(os.listdir(src_path)):
            if not filename.endswith("Drive.Log"):
                continue

            day_prefix = filename.replace("Drive.Log", "")
            if not re.match(r"^\d{2}$", day_prefix):
                continue

            src_file = os.path.join(src_path, filename)
            date_dir = get_date_from_log(src_file, day_prefix)

            if not date_dir:
                print("  SKIP: Cannot determine date for {}".format(filename))
                continue

            dst_dir = os.path.join(DEV_LOGS_DIR, machine_id, date_dir)
            dst_file = os.path.join(dst_dir, filename)

            os.makedirs(dst_dir, exist_ok=True)

            # Check if already copied and same size
            if os.path.exists(dst_file):
                if os.path.getsize(dst_file) == os.path.getsize(src_file):
                    print("  EXISTS: {} -> {}/{}".format(filename, date_dir, filename))
                    skipped += 1
                    continue

            shutil.copy2(src_file, dst_file)
            size_mb = os.path.getsize(dst_file) / (1024 * 1024)
            print("  COPY: {} -> {}/{} ({:.1f} MB)".format(filename, date_dir, filename, size_mb))
            copied += 1

    print()
    print("Done: {} files copied, {} skipped (already exist)".format(copied, skipped))


LASER_SOURCE_DIR = os.path.join(PROJECT_ROOT, "original_logs", "laser_logs")
LASER_XY_SOURCE_DIR = os.path.join(PROJECT_ROOT, "original_logs", "laser_drill_xy_para_file")

# Matches both production format ({YYYYMMDD}_{component}.log) and
# legacy dev format ({prefix}_{YYYYMMDD}_{component}.txt).
SYS_LOG_PATTERN = re.compile(r"^(?:\d+_)?(\d{8})_\w+\.(txt|log)$")
PROC_TIME_PATTERN = re.compile(r"ProcTime(End|Start)\.(txt|log)$", re.IGNORECASE)


LASER_PROG_DIR_PATTERN = re.compile(r"^(L\d+)-DRILL-PROGRAM-LOG-\d+$")


def extract_laser_machine_id(dirname):
    """Extract laser machine ID from directory name like 'L2-logs'.

    Excludes legacy program log directories (L2-DRILL-PROGRAM-LOG-YYMMDD)
    which are handled separately.
    """
    if LASER_PROG_DIR_PATTERN.match(dirname):
        return None
    match = re.match(r"^(L\d+)-", dirname)
    return match.group(1) if match else None


def copy_if_needed(src_file, dst_file):
    """Copy file if destination doesn't exist or size differs. Returns 'copied' or 'skipped'."""
    if os.path.exists(dst_file) and os.path.getsize(dst_file) == os.path.getsize(src_file):
        return "skipped"
    os.makedirs(os.path.dirname(dst_file), exist_ok=True)
    shutil.copy2(src_file, dst_file)
    return "copied"


def classify_laser_files(src_dir, machine_id):
    """Classify and copy laser files from a flat directory into dev_logs structure.

    Handles three file types:
      - System logs (1448_20260407_ClsLaserCom.txt) -> dev_logs/{id}/{YYYYMMDD}/
      - Program logs (ProcTimeEnd/Start.txt)        -> dev_logs/{id}/programs/
      - LSR coordinate files (*.lsr)                 -> dev_logs/{id}/lsr_files/

    Returns (copied, skipped) counts.
    """
    copied = 0
    skipped = 0

    for filename in sorted(os.listdir(src_dir)):
        filepath = os.path.join(src_dir, filename)
        if not os.path.isfile(filepath):
            continue

        # System logs: {number}_{YYYYMMDD}_{component}.txt
        sys_match = SYS_LOG_PATTERN.match(filename)
        if sys_match:
            date_str = sys_match.group(1)
            dst_file = os.path.join(DEV_LOGS_DIR, machine_id, date_str, filename)
            result = copy_if_needed(filepath, dst_file)
            label = "{}/{}/{}".format(machine_id, date_str, filename)
            if result == "copied":
                print("  COPY:   {} -> {}".format(filename, label))
                copied += 1
            else:
                print("  EXISTS: {} -> {}".format(filename, label))
                skipped += 1
            continue

        # Program logs: *ProcTimeEnd.txt, *ProcTimeStart.txt
        if PROC_TIME_PATTERN.search(filename):
            dst_file = os.path.join(DEV_LOGS_DIR, machine_id, "programs", filename)
            result = copy_if_needed(filepath, dst_file)
            label = "{}/programs/{}".format(machine_id, filename)
            if result == "copied":
                print("  COPY:   {} -> {}".format(filename, label))
                copied += 1
            else:
                print("  EXISTS: {} -> {}".format(filename, label))
                skipped += 1
            continue

        # LSR coordinate files
        if filename.lower().endswith(".lsr"):
            dst_file = os.path.join(DEV_LOGS_DIR, machine_id, "lsr_files", filename)
            result = copy_if_needed(filepath, dst_file)
            label = "{}/lsr_files/{}".format(machine_id, filename)
            if result == "copied":
                print("  COPY:   {} -> {}".format(filename, label))
                copied += 1
            else:
                print("  EXISTS: {} -> {}".format(filename, label))
                skipped += 1
            continue

    return copied, skipped


def setup_laser_dev_logs():
    """Copy laser log files into dev_logs directory structure.

    Supports two source layouts:
      1. Per-machine subdirectories: original_logs/laser_logs/L2-logs/, L3-logs/
         (all file types mixed in each subdirectory)
      2. Legacy flat layout: files directly in original_logs/laser_logs/ (defaults to L2)
    """
    if not os.path.isdir(LASER_SOURCE_DIR):
        print("SKIP: Laser source not found: {}".format(LASER_SOURCE_DIR))
        return

    print()
    print("=== Laser Drill Logs ===")
    print("Source: {}".format(LASER_SOURCE_DIR))
    print("Target: {}".format(DEV_LOGS_DIR))
    print()

    total_copied = 0
    total_skipped = 0

    # 1. Scan per-machine subdirectories (e.g. L2-logs/, L3-logs/)
    found_machine_dirs = False
    for dirname in sorted(os.listdir(LASER_SOURCE_DIR)):
        dirpath = os.path.join(LASER_SOURCE_DIR, dirname)
        if not os.path.isdir(dirpath):
            continue

        machine_id = extract_laser_machine_id(dirname)
        if not machine_id:
            continue

        found_machine_dirs = True
        print("[{}] Scanning {}".format(machine_id, dirname))
        c, s = classify_laser_files(dirpath, machine_id)
        total_copied += c
        total_skipped += s

    # 2. Legacy: flat files in laser_logs/ (fallback to L2)
    has_flat_files = False
    for filename in sorted(os.listdir(LASER_SOURCE_DIR)):
        filepath = os.path.join(LASER_SOURCE_DIR, filename)
        if not os.path.isfile(filepath):
            continue
        if SYS_LOG_PATTERN.match(filename) or PROC_TIME_PATTERN.search(filename) or filename.lower().endswith(".lsr"):
            has_flat_files = True
            break

    if has_flat_files:
        print("[L2] Scanning flat files in laser_logs/ (legacy)")
        c, s = classify_laser_files(LASER_SOURCE_DIR, "L2")
        total_copied += c
        total_skipped += s

    # 3. Legacy: program log directories (L2-DRILL-PROGRAM-LOG-YYMMDD/)
    for dirname in sorted(os.listdir(LASER_SOURCE_DIR)):
        dirpath = os.path.join(LASER_SOURCE_DIR, dirname)
        if not os.path.isdir(dirpath):
            continue
        prog_match = LASER_PROG_DIR_PATTERN.match(dirname)
        if not prog_match:
            continue

        machine_id = prog_match.group(1)
        dst_prog_dir = os.path.join(DEV_LOGS_DIR, machine_id, "programs")

        for filename in sorted(os.listdir(dirpath)):
            src_file = os.path.join(dirpath, filename)
            if not os.path.isfile(src_file):
                continue

            dst_file = os.path.join(dst_prog_dir, filename)
            result = copy_if_needed(src_file, dst_file)
            if result == "copied":
                print("  COPY:   {}/{} -> {}/programs/{}".format(dirname, filename, machine_id, filename))
                total_copied += 1
            else:
                print("  EXISTS: {}/{} -> {}/programs/{}".format(dirname, filename, machine_id, filename))
                total_skipped += 1

    # 4. Legacy: LSR files from laser_drill_xy_para_file/
    if os.path.isdir(LASER_XY_SOURCE_DIR):
        machine_id = "L2"
        dst_lsr_dir = os.path.join(DEV_LOGS_DIR, machine_id, "lsr_files")
        os.makedirs(dst_lsr_dir, exist_ok=True)

        for filename in sorted(os.listdir(LASER_XY_SOURCE_DIR)):
            if not filename.lower().endswith((".lsr", ".txt")):
                continue

            src_file = os.path.join(LASER_XY_SOURCE_DIR, filename)
            if not os.path.isfile(src_file):
                continue

            dst_file = os.path.join(dst_lsr_dir, filename)
            result = copy_if_needed(src_file, dst_file)
            if result == "copied":
                print("  COPY:   {} -> lsr_files/{}".format(filename, filename))
                total_copied += 1
            else:
                print("  EXISTS: {} -> lsr_files/{}".format(filename, filename))
                total_skipped += 1

    print()
    print("Laser: {} files copied, {} skipped".format(total_copied, total_skipped))


if __name__ == "__main__":
    setup_dev_logs()
    setup_laser_dev_logs()

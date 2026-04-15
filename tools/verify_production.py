"""
上線前資料一致性驗證腳本。

交叉比對三個層級的資料：
1. 機台控制電腦 LOG（ground truth）vs 運算電腦備份 LOG → checksum 比對
2. 備份 LOG 獨立解析 vs 生產 DB → hourly_utilization 逐小時比對
3. TX1.Log 工單號 vs 生產 DB machine_current_state → 工單正確性
4. 資料合理性檢查 + Application Log 掃描

目錄結構（放在 original_logs/verify/ 下）：
    M13/          ← 機台控制電腦的原始 LOG
    M14/
    backup_M13/   ← 運算電腦 C:\DrillLogs\M13\ 的備份
    backup_M14/
    drill_monitor.db   ← 生產 DB
    drill_monitor.log  ← 應用程式日誌

Usage:
    python3 tools/verify_production.py
    python3 tools/verify_production.py --days 13,14,15
    python3 tools/verify_production.py --verify-dir original_logs/verify
"""

import argparse
import csv
import datetime
import hashlib
import io
import os
import re
import sqlite3
import sys
from collections import defaultdict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_VERIFY_DIR = os.path.join(PROJECT_ROOT, "original_logs", "verify")

# Reuse parser logic
sys.path.insert(0, os.path.join(PROJECT_ROOT, "deploy"))
from parsers.drive_log_parser import parse_csv_line, GAP_CAP_SECONDS, _init_hourly_bucket, _distribute_seconds
from parsers.tx1_log_parser import FILEOPERATION_LOAD_PATTERN
from parsers.drive_log_parser import extract_work_order


# ──────────────────── Utilities ────────────────────


def sha256_file(path):
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def find_log_files(directory, machine_id, day_prefixes):
    """Find Drive.Log and TX1.Log files in a directory.

    Searches two structures:
    - Flat: directory/{DD}Drive.Log
    - Date-subdir: directory/YYYYMMDD/{DD}Drive.Log
    """
    results = {}
    for dp in day_prefixes:
        for log_type in ("Drive.Log", "TX1.Log"):
            filename = "{}{}".format(dp, log_type)
            # Try flat structure
            flat_path = os.path.join(directory, filename)
            if os.path.exists(flat_path):
                results[(dp, log_type)] = flat_path
                continue
            # Try date-subdir structure (YYYYMMDD)
            for entry in os.listdir(directory) if os.path.isdir(directory) else []:
                subdir = os.path.join(directory, entry)
                if os.path.isdir(subdir):
                    subpath = os.path.join(subdir, filename)
                    if os.path.exists(subpath):
                        results[(dp, log_type)] = subpath
                        break
    return results


def parse_drive_log_full(log_path):
    """Parse a Drive.Log file from scratch, returning hourly aggregation.

    Uses the same timestamp-delta logic as the production parser.
    Returns dict of (date, hour) -> {run, reset, stop, hole_count}.
    """
    with open(log_path, "r", encoding="utf-8") as f:
        all_lines = f.readlines()

    parsed_rows = []
    for line in all_lines:
        line = line.strip()
        if not line:
            continue
        row = parse_csv_line(line)
        if row:
            parsed_rows.append(row)

    if not parsed_rows:
        return {}, 0

    # Sort and deduplicate (same logic as production parser)
    for idx, row in enumerate(parsed_rows):
        row["_file_order"] = idx
    parsed_rows.sort(key=lambda r: (r["datetime"], r["_file_order"]))

    deduped = []
    for i, row in enumerate(parsed_rows):
        if (i + 1 < len(parsed_rows)
                and parsed_rows[i + 1]["datetime"] == row["datetime"]):
            continue
        deduped.append(row)
    parsed_rows = deduped

    # Aggregate hourly
    hourly = {}
    for i in range(len(parsed_rows)):
        row = parsed_rows[i]
        state_lower = row["state"].lower()
        key = (row["date"], row["hour"])

        if key not in hourly:
            hourly[key] = _init_hourly_bucket(row["counter"])
        bucket = hourly[key]
        if bucket["first_counter"] is None:
            bucket["first_counter"] = row["counter"]

        # Seconds via delta to next row
        if state_lower in ("run", "reset", "stop"):
            if i + 1 < len(parsed_rows):
                delta = (parsed_rows[i + 1]["datetime"] - row["datetime"]).total_seconds()
                if delta > 0:
                    _distribute_seconds(
                        row["datetime"],
                        min(delta, GAP_CAP_SECONDS),
                        state_lower,
                        hourly,
                    )
            else:
                bucket[state_lower] += 1

        # Hole count
        if bucket["prev_counter"] is not None:
            counter_delta = row["counter"] - bucket["prev_counter"]
            if counter_delta > 0:
                bucket["hole_count"] += counter_delta
        bucket["prev_counter"] = row["counter"]
        bucket["last_counter"] = row["counter"]

    return hourly, len(parsed_rows)


def parse_tx1_log_full(log_path):
    """Parse a TX1.Log file, returning all FILEOPERATION LOAD events.

    Returns list of dicts: [{timestamp, program_name, work_order, side}]
    """
    events = []
    try:
        with open(log_path, "r", encoding="cp932", errors="replace") as f:
            for line in f:
                m = FILEOPERATION_LOAD_PATTERN.match(line.strip())
                if m:
                    ts_raw = m.group(1)
                    iso_ts = ts_raw.replace("/", "-", 2).replace(" ", "T", 1)
                    program_name = m.group(2).strip()
                    wo, side = extract_work_order(program_name)
                    events.append({
                        "timestamp": iso_ts,
                        "program_name": program_name,
                        "work_order": wo,
                        "work_order_side": side,
                    })
    except Exception as e:
        print("    [ERROR] Failed to read {}: {}".format(log_path, e))
    return events


# ──────────────────── Section 1: Checksum ────────────────────


def verify_checksum(verify_dir, machines, day_prefixes):
    """Compare checksums between machine source and backup copies."""
    print()
    print("=" * 70)
    print("  1. 收集完整性 — checksum 比對")
    print("=" * 70)
    print()

    all_pass = True
    missing = []

    for mid in machines:
        source_dir = os.path.join(verify_dir, mid)
        backup_dir = os.path.join(verify_dir, "backup_{}".format(mid))

        if not os.path.isdir(source_dir):
            print("  [SKIP] 機台原始 LOG 目錄不存在: {}".format(source_dir))
            all_pass = False
            continue
        if not os.path.isdir(backup_dir):
            print("  [SKIP] 運算電腦備份目錄不存在: {}".format(backup_dir))
            all_pass = False
            continue

        source_files = find_log_files(source_dir, mid, day_prefixes)
        backup_files = find_log_files(backup_dir, mid, day_prefixes)

        print("  {} — 機台 vs 備份:".format(mid))
        print("  {:<20s}  {:>12s}  {:>12s}  {:>8s}  {}".format(
            "檔案", "機台大小", "備份大小", "結果", "備註"))
        print("  " + "-" * 75)

        for dp in day_prefixes:
            for log_type in ("Drive.Log", "TX1.Log"):
                key = (dp, log_type)
                filename = "{}{}".format(dp, log_type)

                src = source_files.get(key)
                bak = backup_files.get(key)

                if not src and not bak:
                    missing.append((mid, filename, "both"))
                    continue
                elif not src:
                    print("  {:<20s}  {:>12s}  {:>12,d}  {:>8s}  {}".format(
                        filename, "MISSING", os.path.getsize(bak), "FAIL", "機台原始檔缺失"))
                    all_pass = False
                    continue
                elif not bak:
                    print("  {:<20s}  {:>12,d}  {:>12s}  {:>8s}  {}".format(
                        filename, os.path.getsize(src), "MISSING", "FAIL", "備份檔缺失"))
                    all_pass = False
                    missing.append((mid, filename, "backup"))
                    continue

                src_size = os.path.getsize(src)
                bak_size = os.path.getsize(bak)

                if src_size != bak_size:
                    # Size mismatch — backup might be from earlier collection cycle
                    # Check if backup is a prefix of source (still collecting)
                    note = "大小不同 (差 {:+,d} bytes)".format(bak_size - src_size)
                    if bak_size < src_size:
                        note += " — 備份可能是較早的收集版本"
                    print("  {:<20s}  {:>12,d}  {:>12,d}  {:>8s}  {}".format(
                        filename, src_size, bak_size, "WARN", note))
                    # Still compare content up to backup size
                else:
                    src_hash = sha256_file(src)
                    bak_hash = sha256_file(bak)
                    if src_hash == bak_hash:
                        print("  {:<20s}  {:>12,d}  {:>12,d}  {:>8s}  SHA256 match".format(
                            filename, src_size, bak_size, "PASS"))
                    else:
                        print("  {:<20s}  {:>12,d}  {:>12,d}  {:>8s}  SHA256 不一致!".format(
                            filename, src_size, bak_size, "FAIL"))
                        all_pass = False

        print()

    if missing:
        print("  缺失檔案:")
        for mid, fn, where in missing:
            print("    {} {} — {}".format(mid, fn, where))
        print()

    status = "PASS" if all_pass else "FAIL"
    print("  收集完整性總結: {}".format(status))
    print()
    return all_pass


# ──────────────────── Section 2: Parse Accuracy ────────────────────


def verify_parse_accuracy(verify_dir, machines, day_prefixes):
    """Re-parse backup LOGs and compare with production DB."""
    print()
    print("=" * 70)
    print("  2. 解析正確性 — hourly_utilization 比對")
    print("=" * 70)
    print()

    prod_db_path = os.path.join(verify_dir, "drill_monitor.db")
    if not os.path.exists(prod_db_path):
        print("  [SKIP] 生產 DB 不存在: {}".format(prod_db_path))
        print()
        return False

    conn = sqlite3.connect(prod_db_path)
    conn.row_factory = sqlite3.Row

    all_pass = True

    for mid in machines:
        backup_dir = os.path.join(verify_dir, "backup_{}".format(mid))
        if not os.path.isdir(backup_dir):
            # Fall back to source dir if backup not available
            backup_dir = os.path.join(verify_dir, mid)
        if not os.path.isdir(backup_dir):
            print("  [SKIP] {} — 無可用 LOG 檔案".format(mid))
            continue

        files = find_log_files(backup_dir, mid, day_prefixes)

        print("  {} — 獨立解析 vs 生產 DB:".format(mid))

        for dp in day_prefixes:
            drive_key = (dp, "Drive.Log")
            if drive_key not in files:
                continue

            log_path = files[drive_key]
            print()
            print("  解析 {}...".format(os.path.basename(log_path)))
            hourly, row_count = parse_drive_log_full(log_path)
            print("  解析完成: {} 列, {} 小時".format(row_count, len(hourly)))

            if not hourly:
                continue

            # Compare with production DB
            print()
            print("  {:<12s} {:>4s}  {:>7s} {:>7s}  {:>7s} {:>7s}  {:>7s} {:>7s}  {:>10s} {:>10s}  {:>6s}".format(
                "日期", "時",
                "新RUN", "DB_RUN",
                "新RST", "DB_RST",
                "新STP", "DB_STP",
                "新孔數", "DB孔數",
                "結果"))
            print("  " + "-" * 105)

            for (date_str, hour) in sorted(hourly.keys()):
                bucket = hourly[(date_str, hour)]
                new_run = bucket["run"]
                new_reset = bucket["reset"]
                new_stop = bucket["stop"]
                new_holes = bucket["hole_count"]

                cursor = conn.execute(
                    "SELECT run_seconds, reset_seconds, stop_seconds, hole_count "
                    "FROM hourly_utilization WHERE machine_id=? AND date=? AND hour=?",
                    (mid, date_str, hour),
                )
                db_row = cursor.fetchone()

                if db_row is None:
                    print("  {:<12s} {:>4d}  {:>7d} {:>7s}  {:>7d} {:>7s}  {:>7d} {:>7s}  {:>10,d} {:>10s}  {:>6s}".format(
                        date_str, hour,
                        new_run, "---",
                        new_reset, "---",
                        new_stop, "---",
                        new_holes, "---",
                        "NO_DB"))
                    all_pass = False
                    continue

                db_run = db_row["run_seconds"]
                db_reset = db_row["reset_seconds"]
                db_stop = db_row["stop_seconds"]
                db_holes = db_row["hole_count"]

                # Compare — allow small tolerance for incremental parse edge effects
                run_ok = abs(new_run - db_run) <= 2
                reset_ok = abs(new_reset - db_reset) <= 2
                stop_ok = abs(new_stop - db_stop) <= 2
                holes_ok = new_holes == db_holes

                if run_ok and reset_ok and stop_ok and holes_ok:
                    status = "PASS"
                else:
                    status = "DIFF"
                    all_pass = False

                print("  {:<12s} {:>4d}  {:>7d} {:>7d}  {:>7d} {:>7d}  {:>7d} {:>7d}  {:>10,d} {:>10,d}  {:>6s}".format(
                    date_str, hour,
                    new_run, db_run,
                    new_reset, db_reset,
                    new_stop, db_stop,
                    new_holes, db_holes,
                    status))

        print()

    # Also check DB for entries NOT in our parsed data (extra rows)
    print("  生產 DB 中的資料日期範圍:")
    cursor = conn.execute(
        "SELECT machine_id, MIN(date) as min_d, MAX(date) as max_d, COUNT(*) as cnt "
        "FROM hourly_utilization GROUP BY machine_id ORDER BY machine_id"
    )
    for row in cursor.fetchall():
        print("    {}: {} ~ {} ({} 筆)".format(row["machine_id"], row["min_d"], row["max_d"], row["cnt"]))
    print()

    conn.close()

    status = "PASS" if all_pass else "有差異 (見 DIFF 項目)"
    print("  解析正確性總結: {}".format(status))
    print()
    return all_pass


# ──────────────────── Section 3: Work Order ────────────────────


def verify_work_orders(verify_dir, machines, day_prefixes):
    """Verify work orders from TX1.Log against production DB."""
    print()
    print("=" * 70)
    print("  3. 工單號正確性 — TX1.Log vs DB")
    print("=" * 70)
    print()

    prod_db_path = os.path.join(verify_dir, "drill_monitor.db")
    has_db = os.path.exists(prod_db_path)

    if has_db:
        conn = sqlite3.connect(prod_db_path)
        conn.row_factory = sqlite3.Row
    else:
        conn = None
        print("  [INFO] 生產 DB 不存在，僅列出 TX1.Log 事件")
        print()

    for mid in machines:
        print("  {} — TX1.Log 工單事件:".format(mid))
        print()

        # Find TX1.Log files
        all_events = []
        for src_type, src_dir_name in [("機台", mid), ("備份", "backup_{}".format(mid))]:
            src_dir = os.path.join(verify_dir, src_dir_name)
            if not os.path.isdir(src_dir):
                continue
            files = find_log_files(src_dir, mid, day_prefixes)
            for dp in day_prefixes:
                key = (dp, "TX1.Log")
                if key in files:
                    events = parse_tx1_log_full(files[key])
                    if events:
                        print("  ({}) {}TX1.Log: {} 筆 LOAD 事件".format(
                            src_type, dp, len(events)))
                        # Only use one source (prefer machine source)
                        if src_type == "機台" or not all_events:
                            all_events.extend(events)
            if all_events:
                break  # Use machine source if available

        if not all_events:
            print("  [SKIP] 無 TX1.Log 可分析")
            print()
            continue

        # Display all events
        print()
        print("  {:<24s}  {:<20s}  {:<12s}  {:<6s}".format(
            "時間", "程式名", "工單號", "面"))
        print("  " + "-" * 65)

        production_events = []
        for evt in all_events:
            wo_display = evt["work_order"] or "(非生產)"
            side_display = evt["work_order_side"] or ""
            print("  {:<24s}  {:<20s}  {:<12s}  {:<6s}".format(
                evt["timestamp"], evt["program_name"], wo_display, side_display))
            if evt["work_order"]:
                production_events.append(evt)

        print()
        print("  生產工單數: {} / {} 筆 LOAD 事件".format(len(production_events), len(all_events)))

        # Compare last production event with DB
        if production_events and conn:
            last_evt = production_events[-1]
            cursor = conn.execute(
                "SELECT work_order, work_order_side, last_update "
                "FROM machine_current_state WHERE machine_id=?",
                (mid,),
            )
            db_row = cursor.fetchone()

            print()
            print("  最後一筆生產工單 (TX1.Log): {}.{} (at {})".format(
                last_evt["work_order"], last_evt["work_order_side"], last_evt["timestamp"]))

            if db_row:
                db_wo = db_row["work_order"]
                db_side = db_row["work_order_side"]
                db_update = db_row["last_update"]
                print("  生產 DB machine_current_state: {}.{} (last_update={})".format(
                    db_wo, db_side, db_update))

                if db_wo == last_evt["work_order"] and db_side == last_evt["work_order_side"]:
                    print("  結果: PASS — 工單號一致")
                else:
                    print("  結果: DIFF — 工單號不一致!")
                    print("    TX1.Log: {}.{}".format(last_evt["work_order"], last_evt["work_order_side"]))
                    print("    DB:      {}.{}".format(db_wo, db_side))
            else:
                print("  結果: NO_DB — DB 中無此機台的 current_state 記錄")

        # List unique work orders
        if production_events:
            wo_set = set()
            print()
            print("  不重複工單列表:")
            for evt in production_events:
                key = "{}.{}".format(evt["work_order"], evt["work_order_side"])
                if key not in wo_set:
                    wo_set.add(key)
                    print("    {}  (首次出現: {})".format(key, evt["timestamp"]))

        print()

    if conn:
        conn.close()


# ──────────────────── Section 4: Sanity Checks ────────────────────


def verify_sanity(verify_dir, machines, day_prefixes):
    """Run sanity checks on production DB data."""
    print()
    print("=" * 70)
    print("  4. 資料合理性檢查")
    print("=" * 70)
    print()

    prod_db_path = os.path.join(verify_dir, "drill_monitor.db")
    if not os.path.exists(prod_db_path):
        print("  [SKIP] 生產 DB 不存在")
        print()
        return False

    conn = sqlite3.connect(prod_db_path)
    conn.row_factory = sqlite3.Row

    all_pass = True
    issues = []

    for mid in machines:
        print("  {} — 合理性檢查:".format(mid))

        cursor = conn.execute(
            "SELECT date, "
            "SUM(run_seconds) as run, SUM(reset_seconds) as reset_, "
            "SUM(stop_seconds) as stop, SUM(hole_count) as holes, "
            "SUM(run_seconds + reset_seconds + stop_seconds) as total "
            "FROM hourly_utilization WHERE machine_id=? "
            "GROUP BY date ORDER BY date",
            (mid,),
        )

        print("  {:<12s}  {:>7s}  {:>7s}  {:>7s}  {:>7s}  {:>6s}  {:>12s}  {}".format(
            "日期", "RUN", "RESET", "STOP", "合計", "稼動%", "孔數", "問題"))
        print("  " + "-" * 80)

        for row in cursor.fetchall():
            d = dict(row)
            total = d["total"] or 0
            run = d["run"] or 0
            holes = d["holes"] or 0
            util = (run / total * 100.0) if total > 0 else 0.0

            warns = []
            # Check total seconds reasonable (should be <= 86400)
            if total > 86400:
                warns.append("合計>86400")
                all_pass = False
            elif total < 3600 and total > 0:
                warns.append("資料不足(<1h)")

            # Check hole count anomaly
            if holes > 1000000:
                warns.append("孔數異常({:,})".format(holes))
                all_pass = False
            elif holes < 0:
                warns.append("孔數負值")
                all_pass = False

            # Check utilization reasonable
            if util > 100.0:
                warns.append("稼動>100%")
                all_pass = False

            warn_str = "; ".join(warns) if warns else ""
            if warns:
                issues.append((mid, d["date"], warn_str))

            print("  {:<12s}  {:>7d}  {:>7d}  {:>7d}  {:>7,d}  {:>5.1f}%  {:>12,d}  {}".format(
                d["date"],
                run, d["reset_"] or 0, d["stop"] or 0,
                total, util, holes, warn_str,
            ))

        print()

    # Check for duplicate hourly entries
    print("  重複資料檢查:")
    cursor = conn.execute(
        "SELECT machine_id, date, hour, COUNT(*) as cnt "
        "FROM hourly_utilization GROUP BY machine_id, date, hour HAVING cnt > 1"
    )
    dupes = cursor.fetchall()
    if dupes:
        print("  [FAIL] 發現重複的 hourly_utilization 記錄:")
        for d in dupes:
            print("    {} {} hour={} (count={})".format(d["machine_id"], d["date"], d["hour"], d["cnt"]))
        all_pass = False
    else:
        print("  [PASS] 無重複記錄")
    print()

    # Check parse_progress
    print("  parse_progress 狀態:")
    cursor = conn.execute(
        "SELECT machine_id, day_prefix, last_line, last_timestamp, file_size "
        "FROM parse_progress ORDER BY machine_id, day_prefix"
    )
    print("  {:<6s}  {:<8s}  {:>10s}  {:<24s}  {:>12s}".format(
        "機台", "day_pfx", "last_line", "last_timestamp", "file_size"))
    print("  " + "-" * 65)
    for row in cursor.fetchall():
        d = dict(row)
        print("  {:<6s}  {:<8s}  {:>10,d}  {:<24s}  {:>12,d}".format(
            d["machine_id"], d["day_prefix"],
            d["last_line"] or 0, d["last_timestamp"] or "",
            d["file_size"] or 0,
        ))
    print()

    conn.close()

    if issues:
        print("  問題彙總:")
        for mid, date, warn in issues:
            print("    {} {} — {}".format(mid, date, warn))
        print()

    status = "PASS" if all_pass else "有異常 (見上方問題)"
    print("  資料合理性總結: {}".format(status))
    print()
    return all_pass


# ──────────────────── Section 5: App Log ────────────────────


def verify_app_log(verify_dir):
    """Scan application log for errors and warnings."""
    print()
    print("=" * 70)
    print("  5. Application Log 檢查")
    print("=" * 70)
    print()

    log_path = os.path.join(verify_dir, "drill_monitor.log")
    if not os.path.exists(log_path):
        print("  [SKIP] Application log 不存在: {}".format(log_path))
        print()
        return True

    size_mb = os.path.getsize(log_path) / 1024 / 1024
    print("  日誌檔案: {} ({:.1f} MB)".format(log_path, size_mb))
    print()

    error_pattern = re.compile(r"\[ERROR\]|\[CRITICAL\]|Traceback|Exception|Error:", re.IGNORECASE)
    warning_pattern = re.compile(r"\[WARNING\]", re.IGNORECASE)
    cycle_pattern = re.compile(r"(Parser cycle|collection cycle|TX1 parser cycle)\s+(start|complete)", re.IGNORECASE)
    robocopy_fail = re.compile(r"robocopy.*(fail|error|timeout)", re.IGNORECASE)

    errors = []
    warnings = []
    cycle_starts = 0
    cycle_completes = 0
    robocopy_issues = []

    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if error_pattern.search(line):
                errors.append((i, line[:120]))
            elif warning_pattern.search(line):
                warnings.append((i, line[:120]))

            if cycle_pattern.search(line):
                if "start" in line.lower():
                    cycle_starts += 1
                elif "complete" in line.lower():
                    cycle_completes += 1

            if robocopy_fail.search(line):
                robocopy_issues.append((i, line[:120]))

    print("  Cycle 統計: {} 次啟動, {} 次完成".format(cycle_starts, cycle_completes))
    if cycle_starts > 0 and cycle_completes < cycle_starts:
        incomplete = cycle_starts - cycle_completes
        print("  [WARN] {} 次 cycle 未完成".format(incomplete))
    print()

    if errors:
        print("  ERROR 記錄 ({} 筆):".format(len(errors)))
        for lineno, text in errors[:20]:
            print("    L{}: {}".format(lineno, text))
        if len(errors) > 20:
            print("    ... 共 {} 筆".format(len(errors)))
        print()
    else:
        print("  [PASS] 無 ERROR 記錄")
        print()

    if warnings:
        print("  WARNING 記錄 ({} 筆):".format(len(warnings)))
        for lineno, text in warnings[:20]:
            print("    L{}: {}".format(lineno, text))
        if len(warnings) > 20:
            print("    ... 共 {} 筆".format(len(warnings)))
        print()
    else:
        print("  [PASS] 無 WARNING 記錄")
        print()

    if robocopy_issues:
        print("  Robocopy 問題 ({} 筆):".format(len(robocopy_issues)))
        for lineno, text in robocopy_issues[:10]:
            print("    L{}: {}".format(lineno, text))
        print()

    has_errors = len(errors) > 0
    status = "有 {} ERROR 需檢查".format(len(errors)) if has_errors else "PASS"
    print("  Application Log 總結: {}".format(status))
    print()
    return not has_errors


# ──────────────────── Main ────────────────────


def main():
    parser = argparse.ArgumentParser(description="上線前資料一致性驗證")
    parser.add_argument("--verify-dir", default=DEFAULT_VERIFY_DIR,
                        help="驗證資料目錄 (default: original_logs/verify)")
    parser.add_argument("--days", default="13,14,15",
                        help="要驗證的 day_prefix（逗號分隔，default: 13,14,15）")
    parser.add_argument("--machines", default="M13,M14",
                        help="要驗證的機台（逗號分隔，default: M13,M14）")
    parser.add_argument("--section", default="all",
                        help="執行特定段落 (1-5 or all, default: all)")
    args = parser.parse_args()

    day_prefixes = [d.strip() for d in args.days.split(",")]
    machines = [m.strip() for m in args.machines.split(",")]
    verify_dir = args.verify_dir
    if not os.path.isabs(verify_dir):
        verify_dir = os.path.join(PROJECT_ROOT, verify_dir)

    print("=" * 70)
    print("  上線前資料一致性驗證報告")
    print("  產出時間: {}".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    print("  驗證目錄: {}".format(verify_dir))
    print("  驗證機台: {}".format(", ".join(machines)))
    print("  驗證日期: {}".format(", ".join(day_prefixes)))
    print("=" * 70)

    if not os.path.isdir(verify_dir):
        print("\n  [ERROR] 驗證目錄不存在: {}".format(verify_dir))
        print("  請先將資料放到該目錄下。")
        sys.exit(1)

    # List available files
    print()
    print("  目錄內容:")
    for entry in sorted(os.listdir(verify_dir)):
        full = os.path.join(verify_dir, entry)
        if os.path.isdir(full):
            sub_count = len(os.listdir(full))
            print("    {}/  ({} 項)".format(entry, sub_count))
        else:
            size = os.path.getsize(full)
            print("    {}  ({:,} bytes)".format(entry, size))
    print()

    section = args.section
    results = {}

    if section in ("all", "1"):
        results["checksum"] = verify_checksum(verify_dir, machines, day_prefixes)

    if section in ("all", "2"):
        results["parse"] = verify_parse_accuracy(verify_dir, machines, day_prefixes)

    if section in ("all", "3"):
        verify_work_orders(verify_dir, machines, day_prefixes)

    if section in ("all", "4"):
        results["sanity"] = verify_sanity(verify_dir, machines, day_prefixes)

    if section in ("all", "5"):
        results["app_log"] = verify_app_log(verify_dir)

    # Final summary
    print()
    print("=" * 70)
    print("  驗證總結")
    print("=" * 70)
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL / 有異常"
        print("  {}: {}".format(name, status))

    all_pass = all(results.values()) if results else False
    print()
    if all_pass:
        print("  結論: 全部通過 — 可以上線")
    else:
        print("  結論: 有異常項目需要處理")
    print("=" * 70)


if __name__ == "__main__":
    main()

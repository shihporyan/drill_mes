"""
分析三天長測 Drive.Log 資料（4/10~4/12）。

直接讀取 original_logs/machine_logs/ 下的原始 LOG 檔，
不使用增量解析器、不修改 DB。輸出文字報告到 stdout。

報告內容：
1. 欄位完整性（23 欄 + M14 額外 hex 欄）
2. 時間連續性（> 2 秒間隔）
3. 每小時稼動率表（RUN/RESET/STOP 秒數）
4. 作業細節欄位彙總（工號、板號、針徑、孔數）
5. 跨日處理驗證

Usage:
    python3 tools/analyze_test_run.py
    python3 tools/analyze_test_run.py --m13 path/to/M13-LOGS --m14 path/to/M14-LOGS
"""

import argparse
import datetime
import os
import sqlite3
import sys
from collections import Counter, defaultdict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Default paths
DEFAULT_M13 = os.path.join(PROJECT_ROOT, "original_logs", "machine_logs", "M13-LOGS")
DEFAULT_M14 = os.path.join(PROJECT_ROOT, "original_logs", "machine_logs", "M14-LOGS")
DEFAULT_CAL_DB = os.path.join(
    PROJECT_ROOT, "original_logs", "machine_logs", "M13M14-CAL-PC-LOG", "drill_monitor.db"
)
DAY_PREFIXES = ["10", "11", "12"]

# Counter deltas larger than this per second are treated as counter
# initialization (e.g., machine restart from 0 to accumulated value),
# not actual drilling.
MAX_HOLE_DELTA_PER_ROW = 10000


# ─────────────────────── Parsing ───────────────────────


def parse_full_line(line):
    """Parse a single Drive.Log CSV line, extracting ALL columns.

    Returns dict with all 23+ fields, or None if unparseable.
    """
    line = line.strip()
    if not line:
        return None

    fields = line.split(",")
    if len(fields) < 23:
        return None

    stripped = [f.strip() for f in fields]

    date_str = stripped[0]
    time_str = stripped[1]
    mode = stripped[2]
    state = stripped[3]

    if state not in ("RUN", "RESET", "STOP"):
        return None

    try:
        dt = datetime.datetime.strptime(
            "{} {}".format(date_str, time_str), "%Y/%m/%d %H:%M:%S"
        )
    except ValueError:
        return None

    try:
        drill_dia = float(stripped[8])
    except (ValueError, IndexError):
        drill_dia = 0.0

    try:
        counter = int(stripped[10])
    except (ValueError, IndexError):
        counter = 0

    try:
        x_coord = float(stripped[5])
    except (ValueError, IndexError):
        x_coord = 0.0

    try:
        y_coord = float(stripped[6])
    except (ValueError, IndexError):
        y_coord = 0.0

    try:
        z_axis = float(stripped[17])
    except (ValueError, IndexError):
        z_axis = 0.0

    return {
        "datetime": dt,
        "iso_date": dt.strftime("%Y-%m-%d"),
        "hour": dt.hour,
        "mode": mode,
        "state": state,
        "program": stripped[4],
        "x": x_coord,
        "y": y_coord,
        "tool_num": stripped[7],
        "drill_dia": drill_dia,
        "msg_code": stripped[9],
        "counter": counter,
        "flags": [stripped[i] for i in range(11, 17)],
        "z_axis": z_axis,
        "reserved": [stripped[i] for i in range(18, min(23, len(stripped)))],
        "extra_cols": [stripped[i] for i in range(23, len(stripped))],
        "total_cols": len(fields),
    }


def read_log_file(path):
    """Read and parse all lines from a Drive.Log file."""
    rows = []
    skipped = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            row = parse_full_line(line)
            if row is None:
                skipped += 1
            else:
                rows.append(row)
    return rows, skipped


# ─────────────────────── Report Sections ───────────────────────


def report_column_completeness(machine_id, all_rows):
    """Section 1: 欄位完整性."""
    print("=" * 70)
    print("  1. 欄位完整性 — {}".format(machine_id))
    print("=" * 70)
    print("  總列數: {:,}".format(len(all_rows)))
    print()

    # Column count distribution
    col_counts = Counter(r["total_cols"] for r in all_rows)
    print("  欄位數量分佈:")
    for cnt, freq in sorted(col_counts.items()):
        print("    {} 欄: {:,} 列".format(cnt, freq))
    print()

    # Per-column analysis
    col_defs = [
        ("col0 日期", lambda r: r["datetime"].strftime("%Y/%m/%d")),
        ("col1 時間", lambda r: r["datetime"].strftime("%H:%M:%S")),
        ("col2 模式", lambda r: r["mode"]),
        ("col3 狀態 ★", lambda r: r["state"]),
        ("col4 程式名", lambda r: r["program"]),
        ("col5 X座標", lambda r: str(r["x"])),
        ("col6 Y座標", lambda r: str(r["y"])),
        ("col7 刀號", lambda r: r["tool_num"]),
        ("col8 針徑mm ★", lambda r: str(r["drill_dia"])),
        ("col9 訊息碼 ★", lambda r: r["msg_code"]),
        ("col10 累計孔數 ★", lambda r: str(r["counter"])),
        ("col11-16 旗標", lambda r: ",".join(r["flags"])),
        ("col17 Z軸", lambda r: str(r["z_axis"])),
        ("col18-22 保留", lambda r: ",".join(r["reserved"])),
    ]

    for name, extractor in col_defs:
        values = [extractor(r) for r in all_rows]
        unique = sorted(set(values))
        n_unique = len(unique)
        examples = unique[:8]
        if n_unique > 8:
            examples_str = ", ".join(examples) + " ... (共 {:,} 種)".format(n_unique)
        else:
            examples_str = ", ".join(examples)
        print("  {:<20s}  不重複: {:>8,}  範例: {}".format(name, n_unique, examples_str))

    # Extra columns (M14)
    has_extra = [r for r in all_rows if r["extra_cols"]]
    if has_extra:
        print()
        print("  額外欄位（col23+）: {:,} 列有額外欄位".format(len(has_extra)))
        sample = has_extra[0]["extra_cols"]
        for i, v in enumerate(sample):
            display = v[:40] + "..." if len(v) > 40 else v
            print("    col{}: {} (長度 {})".format(23 + i, display, len(v)))
    print()


def report_time_continuity(machine_id, all_rows):
    """Section 2: 時間連續性."""
    print("=" * 70)
    print("  2. 時間連續性 — {}".format(machine_id))
    print("=" * 70)

    if len(all_rows) < 2:
        print("  資料不足（< 2 列）")
        print()
        return

    gaps = []
    for i in range(1, len(all_rows)):
        prev_dt = all_rows[i - 1]["datetime"]
        curr_dt = all_rows[i]["datetime"]
        delta = (curr_dt - prev_dt).total_seconds()
        if delta > 2 or delta < 0:
            gaps.append({
                "index": i,
                "before": prev_dt,
                "after": curr_dt,
                "gap_seconds": delta,
            })

    print("  總列數: {:,}  |  時間範圍: {} ~ {}".format(
        len(all_rows),
        all_rows[0]["datetime"].strftime("%m/%d %H:%M:%S"),
        all_rows[-1]["datetime"].strftime("%m/%d %H:%M:%S"),
    ))
    print("  間隔 > 2 秒的數量: {}".format(len(gaps)))
    print()

    if gaps:
        print("  {:<6s} {:<22s} {:<22s} {:>12s}  {}".format(
            "行號", "間隔前", "間隔後", "間隔(秒)", "備註"))
        print("  " + "-" * 80)
        for g in gaps:
            gap_s = g["gap_seconds"]
            note = ""
            if gap_s < 0:
                note = "⚠ 時間倒退"
            elif gap_s > 3600:
                hours = gap_s / 3600
                note = "({:.1f} 小時)".format(hours)
            elif gap_s > 60:
                mins = gap_s / 60
                note = "({:.1f} 分鐘)".format(mins)

            # Check if this is a file boundary
            before_date = g["before"].strftime("%m/%d")
            after_date = g["after"].strftime("%m/%d")
            if before_date != after_date:
                note += " [跨日]"

            print("  {:<6d} {:<22s} {:<22s} {:>12.0f}  {}".format(
                g["index"],
                g["before"].strftime("%Y-%m-%d %H:%M:%S"),
                g["after"].strftime("%Y-%m-%d %H:%M:%S"),
                gap_s,
                note,
            ))
    print()


def report_hourly_utilization(machine_id, all_rows):
    """Section 3: 每小時稼動率表."""
    print("=" * 70)
    print("  3. 稼動率表（每小時）— {}".format(machine_id))
    print("=" * 70)
    print()

    # Aggregate by (date, hour)
    hourly = defaultdict(lambda: {
        "run": 0, "reset": 0, "stop": 0,
        "first_counter": None, "last_counter": None,
    })

    for r in all_rows:
        key = (r["iso_date"], r["hour"])
        bucket = hourly[key]
        state_lower = r["state"].lower()
        bucket[state_lower] += 1

        if bucket["first_counter"] is None:
            bucket["first_counter"] = r["counter"]
        bucket["last_counter"] = r["counter"]

    # Compute hole counts with positive-delta accumulation
    hourly_holes = defaultdict(lambda: {"hole_count": 0, "prev_counter": None})
    for r in all_rows:
        key = (r["iso_date"], r["hour"])
        hh = hourly_holes[key]
        if hh["prev_counter"] is not None:
            delta = r["counter"] - hh["prev_counter"]
            if 0 < delta <= MAX_HOLE_DELTA_PER_ROW:
                hh["hole_count"] += delta
        hh["prev_counter"] = r["counter"]

    # Group by date
    dates = sorted(set(r["iso_date"] for r in all_rows))

    header = "  {:>5s}  {:>6s}  {:>6s}  {:>6s}  {:>6s}  {:>6s}  {:>8s}".format(
        "時", "RUN", "RESET", "STOP", "合計", "稼動%", "孔數")
    divider = "  " + "-" * 60

    for date in dates:
        print("  日期: {}".format(date))
        print(header)
        print(divider)

        day_run = day_reset = day_stop = day_total = day_holes = 0

        for hour in range(24):
            key = (date, hour)
            if key not in hourly:
                continue
            b = hourly[key]
            total = b["run"] + b["reset"] + b["stop"]
            util = (b["run"] / total * 100.0) if total > 0 else 0.0
            holes = hourly_holes[key]["hole_count"]

            print("  {:>5s}  {:>6d}  {:>6d}  {:>6d}  {:>6d}  {:>5.1f}%  {:>8,d}".format(
                "{:02d}:00".format(hour),
                b["run"], b["reset"], b["stop"], total, util, holes,
            ))

            day_run += b["run"]
            day_reset += b["reset"]
            day_stop += b["stop"]
            day_total += total
            day_holes += holes

        day_util = (day_run / day_total * 100.0) if day_total > 0 else 0.0
        print(divider)
        print("  {:>5s}  {:>6d}  {:>6d}  {:>6d}  {:>6d}  {:>5.1f}%  {:>8,d}".format(
            "合計", day_run, day_reset, day_stop, day_total, day_util, day_holes,
        ))
        print()


def report_work_details(machine_id, all_rows):
    """Section 4: 作業細節欄位."""
    print("=" * 70)
    print("  4. 作業細節欄位 — {}".format(machine_id))
    print("=" * 70)
    print()

    # 4a. Program names (工號)
    programs = Counter(r["program"] for r in all_rows)
    print("  程式名（col4）不重複值: {}".format(len(programs)))
    print("  {:<30s}  {:>10s}  {:>6s}".format("程式名", "出現次數", "佔比%"))
    print("  " + "-" * 50)
    total = len(all_rows)
    for prog, count in programs.most_common():
        pct = count / total * 100
        print("  {:<30s}  {:>10,d}  {:>5.1f}%".format(prog, count, pct))
    print()

    # 4b. Work order suffix analysis (板號)
    import re
    wo_pattern = re.compile(r"^O(\d+)\.(\w+)$", re.IGNORECASE)
    wo_suffixes = Counter()
    wo_names = Counter()
    for r in all_rows:
        m = wo_pattern.match(r["program"])
        if m:
            wo_names[m.group(1)] += 1
            wo_suffixes[m.group(2).upper()] += 1

    if wo_suffixes:
        print("  工號後綴分佈:")
        for suffix, count in wo_suffixes.most_common():
            print("    .{}  {:>10,d} 列".format(suffix, count))
        print()
        print("  不重複工號:")
        for wo, count in wo_names.most_common():
            print("    O{}  {:>10,d} 列".format(wo, count))
    else:
        print("  未偵測到符合 O####.X 格式的生產程式")
    print()

    # 4c. Drill diameter (針徑)
    dias = Counter(r["drill_dia"] for r in all_rows)
    print("  針徑（col8）不重複值: {}".format(len(dias)))
    print("  {:<12s}  {:>10s}".format("針徑(mm)", "出現次數"))
    print("  " + "-" * 25)
    for dia, count in sorted(dias.items()):
        print("  {:<12.3f}  {:>10,d}".format(dia, count))
    print()

    # 4d. Message codes (訊息碼)
    msg_codes = Counter(r["msg_code"] for r in all_rows)
    print("  訊息碼（col9）不重複值: {}".format(len(msg_codes)))
    print("  {:<12s}  {:>10s}".format("訊息碼", "出現次數"))
    print("  " + "-" * 25)
    for code, count in msg_codes.most_common(20):
        print("  {:<12s}  {:>10,d}".format(code, count))
    if len(msg_codes) > 20:
        print("  ... 共 {} 種".format(len(msg_codes)))
    print()

    # 4e. Counter (孔數) daily summary — use positive-delta accumulation
    dates = sorted(set(r["iso_date"] for r in all_rows))
    print("  孔數計數器（col10）每日摘要:")
    print("  {:<12s}  {:>14s}  {:>14s}  {:>12s}  {:>12s}".format(
        "日期", "起始值", "結束值", "簡單差值", "累加孔數"))
    print("  " + "-" * 70)
    for date in dates:
        day_rows = [r for r in all_rows if r["iso_date"] == date]
        first_c = day_rows[0]["counter"]
        last_c = day_rows[-1]["counter"]
        simple_delta = last_c - first_c
        # Accumulate only reasonable positive deltas
        acc_holes = 0
        prev_c = None
        for r in day_rows:
            if prev_c is not None:
                d = r["counter"] - prev_c
                if 0 < d <= MAX_HOLE_DELTA_PER_ROW:
                    acc_holes += d
            prev_c = r["counter"]
        note = ""
        if simple_delta < 0 or simple_delta > 1000000:
            note = "  ← counter 初始化/重置"
        print("  {:<12s}  {:>14,d}  {:>14,d}  {:>12,d}  {:>12,d}{}".format(
            date, first_c, last_c, simple_delta, acc_holes, note))
    print()


def report_cross_day(machine_id, files_data):
    """Section 5: 跨日處理驗證."""
    print("=" * 70)
    print("  5. 跨日處理驗證 — {}".format(machine_id))
    print("=" * 70)
    print()

    # Per-file summary
    print("  各檔案時間範圍:")
    print("  {:<18s}  {:>8s}  {:<22s}  {:<22s}".format(
        "檔案", "列數", "第一筆", "最後一筆"))
    print("  " + "-" * 75)

    for filename, rows in files_data:
        if not rows:
            print("  {:<18s}  {:>8d}  (空)".format(filename, 0))
            continue
        print("  {:<18s}  {:>8,d}  {:<22s}  {:<22s}".format(
            filename,
            len(rows),
            rows[0]["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
            rows[-1]["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
        ))
    print()

    # Per-date summary across all files
    all_rows = []
    for _, rows in files_data:
        all_rows.extend(rows)
    all_rows.sort(key=lambda r: r["datetime"])

    dates = sorted(set(r["iso_date"] for r in all_rows))
    print("  各日期涵蓋範圍:")
    print("  {:<12s}  {:>8s}  {:<22s}  {:<22s}".format(
        "日期", "列數", "第一筆", "最後一筆"))
    print("  " + "-" * 70)
    for date in dates:
        day_rows = [r for r in all_rows if r["iso_date"] == date]
        print("  {:<12s}  {:>8,d}  {:<22s}  {:<22s}".format(
            date,
            len(day_rows),
            day_rows[0]["datetime"].strftime("%H:%M:%S"),
            day_rows[-1]["datetime"].strftime("%H:%M:%S"),
        ))
    print()

    # Check monotonicity within each file
    print("  時間單調性檢查:")
    for filename, rows in files_data:
        reversals = 0
        for i in range(1, len(rows)):
            if rows[i]["datetime"] < rows[i - 1]["datetime"]:
                reversals += 1
        status = "✓ 正常" if reversals == 0 else "⚠ {} 處時間倒退".format(reversals)
        print("    {}: {}".format(filename, status))
    print()

    # Cross-file continuity
    print("  檔案銜接檢查:")
    prev_file = None
    prev_last = None
    for filename, rows in files_data:
        if not rows:
            continue
        if prev_last is not None:
            gap = (rows[0]["datetime"] - prev_last).total_seconds()
            if abs(gap - 1.0) < 0.5:
                status = "✓ 無縫銜接 (間隔 {:.0f}s)".format(gap)
            elif gap > 2:
                status = "⚠ 間隔 {:.0f}s ({:.1f} 小時)".format(gap, gap / 3600)
            else:
                status = "間隔 {:.0f}s".format(gap)
            print("    {} → {}: {}".format(prev_file, filename, status))
        prev_file = filename
        prev_last = rows[-1]["datetime"]
    print()


def report_cal_db(cal_db_path):
    """Section 4 補充: 比對運算電腦 DB 內容."""
    print("=" * 70)
    print("  4+. 運算電腦 DB 比對 — {}".format(os.path.basename(cal_db_path)))
    print("=" * 70)
    print()

    if not os.path.exists(cal_db_path):
        print("  DB 檔案不存在: {}".format(cal_db_path))
        print()
        return

    conn = sqlite3.connect(cal_db_path)
    conn.row_factory = sqlite3.Row

    # Tables
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]
    print("  資料表: {}".format(", ".join(tables)))
    print()

    # machine_current_state
    if "machine_current_state" in tables:
        print("  machine_current_state:")
        cursor = conn.execute("SELECT * FROM machine_current_state")
        for row in cursor.fetchall():
            d = dict(row)
            print("    機台={}, 狀態={}, 程式={}, 刀號={}, 針徑={}, 計數器={}".format(
                d.get("machine_id"), d.get("state"), d.get("program"),
                d.get("tool_num"), d.get("drill_dia"), d.get("counter"),
            ))
            print("    工號={}, 板面={}, since={}, last_update={}".format(
                d.get("work_order"), d.get("work_order_side"),
                d.get("since"), d.get("last_update"),
            ))
        print()

    # hourly_utilization summary
    if "hourly_utilization" in tables:
        cursor = conn.execute(
            "SELECT machine_id, date, COUNT(*) as hours, "
            "SUM(run_seconds) as run, SUM(reset_seconds) as reset_, "
            "SUM(stop_seconds) as stop, SUM(hole_count) as holes "
            "FROM hourly_utilization GROUP BY machine_id, date ORDER BY machine_id, date"
        )
        print("  hourly_utilization 摘要:")
        print("  {:<6s}  {:<12s}  {:>4s}  {:>7s}  {:>7s}  {:>7s}  {:>8s}".format(
            "機台", "日期", "時數", "RUN", "RESET", "STOP", "孔數"))
        print("  " + "-" * 60)
        for row in cursor.fetchall():
            d = dict(row)
            print("  {:<6s}  {:<12s}  {:>4d}  {:>7d}  {:>7d}  {:>7d}  {:>8,d}".format(
                d["machine_id"], d["date"], d["hours"],
                d["run"] or 0, d["reset_"] or 0, d["stop"] or 0, d["holes"] or 0,
            ))
        print()

    # parse_progress
    if "parse_progress" in tables:
        print("  parse_progress:")
        cursor = conn.execute("SELECT * FROM parse_progress ORDER BY machine_id, day_prefix")
        print("  {:<6s}  {:<8s}  {:>10s}  {:<22s}  {:>12s}".format(
            "機台", "day_pfx", "last_line", "last_timestamp", "file_size"))
        print("  " + "-" * 65)
        for row in cursor.fetchall():
            d = dict(row)
            print("  {:<6s}  {:<8s}  {:>10d}  {:<22s}  {:>12,d}".format(
                d["machine_id"], d["day_prefix"], d["last_line"] or 0,
                d["last_timestamp"] or "", d["file_size"] or 0,
            ))
        print()

    # state_transitions count
    if "state_transitions" in tables:
        cursor = conn.execute(
            "SELECT machine_id, COUNT(*) as cnt, MIN(timestamp) as first_t, MAX(timestamp) as last_t "
            "FROM state_transitions GROUP BY machine_id"
        )
        print("  state_transitions 摘要:")
        for row in cursor.fetchall():
            d = dict(row)
            print("    {}: {} 筆 ({} ~ {})".format(
                d["machine_id"], d["cnt"], d["first_t"], d["last_t"]))
        print()

    conn.close()


# ─────────────────────── Main ───────────────────────


def analyze_machine(machine_id, log_dir, day_prefixes):
    """Run all analysis sections for one machine."""
    files_data = []
    all_rows = []

    print()
    print("#" * 70)
    print("#  {} — 分析報告".format(machine_id))
    print("#" * 70)

    for dp in day_prefixes:
        filename = "{}Drive.Log".format(dp)
        path = os.path.join(log_dir, filename)
        if not os.path.exists(path):
            print("\n  [跳過] {} 不存在".format(path))
            files_data.append((filename, []))
            continue

        size_mb = os.path.getsize(path) / 1024 / 1024
        print("\n  讀取 {} ({:.1f} MB)...".format(filename, size_mb))
        rows, skipped = read_log_file(path)
        print("  解析: {:,} 列成功, {} 列跳過".format(len(rows), skipped))

        files_data.append((filename, rows))
        all_rows.extend(rows)

    if not all_rows:
        print("\n  無可分析資料")
        return

    # Sort all rows chronologically
    all_rows.sort(key=lambda r: r["datetime"])

    print()
    report_column_completeness(machine_id, all_rows)
    report_time_continuity(machine_id, all_rows)
    report_hourly_utilization(machine_id, all_rows)
    report_work_details(machine_id, all_rows)
    report_cross_day(machine_id, files_data)


def main():
    parser = argparse.ArgumentParser(description="分析三天長測 Drive.Log")
    parser.add_argument("--m13", default=DEFAULT_M13, help="M13 LOG 目錄")
    parser.add_argument("--m14", default=DEFAULT_M14, help="M14 LOG 目錄")
    parser.add_argument("--cal-db", default=DEFAULT_CAL_DB, help="運算電腦 DB 路徑")
    parser.add_argument("--days", default="10,11,12", help="要分析的 day_prefix（逗號分隔）")
    args = parser.parse_args()

    day_prefixes = [d.strip() for d in args.days.split(",")]

    print("=" * 70)
    print("  Drive.Log 三天長測分析報告")
    print("  產出時間: {}".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    print("  分析日期前綴: {}".format(", ".join(day_prefixes)))
    print("=" * 70)

    # M13
    if os.path.isdir(args.m13):
        analyze_machine("M13", args.m13, day_prefixes)
    else:
        print("\n  [跳過] M13 目錄不存在: {}".format(args.m13))

    # M14
    if os.path.isdir(args.m14):
        analyze_machine("M14", args.m14, day_prefixes)
    else:
        print("\n  [跳過] M14 目錄不存在: {}".format(args.m14))

    # Cal DB comparison
    print()
    report_cal_db(args.cal_db)

    print("=" * 70)
    print("  分析完成")
    print("=" * 70)


if __name__ == "__main__":
    main()

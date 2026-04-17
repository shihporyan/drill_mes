"""
USB LOG vs Production DB 交叉驗證。

直接讀取 USB 上的 Drive.Log 原始資料，獨立做完整單次解析，
逐小時比對 Production DB 的 hourly_utilization 資料。

用途：上線前確認稼動率、孔數的正確性。

Usage:
    python3 tools/verify_usb_logs.py
"""

import os
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from parsers.drive_log_parser import parse_csv_line, GAP_CAP_SECONDS, \
    _init_hourly_bucket, _distribute_seconds, extract_work_order
from parsers.tx1_log_parser import FILEOPERATION_LOAD_PATTERN

# ── Configuration ──

USB_ROOT = "/Volumes/NO NAME"

LOG_FILES = {
    "M13": [
        os.path.join(USB_ROOT, "M13-LOGS/0417/20260416/16Drive.Log"),
        os.path.join(USB_ROOT, "M13-LOGS/0417/20260417/17Drive.Log"),
    ],
    "M14": [
        os.path.join(USB_ROOT, "M14-LOGS/0417/20260416/16Drive.Log"),
        os.path.join(USB_ROOT, "M14-LOGS/0417/20260417/17Drive.Log"),
    ],
}

TX1_FILES = {
    "M13": [
        (os.path.join(USB_ROOT, "M13-LOGS/0417-CONTROL/16TX1.Log"), "cal"),
        (os.path.join(USB_ROOT, "M13-LOGS/0417/20260416/16TX1.Log"), "ctrl"),
        (os.path.join(USB_ROOT, "M13-LOGS/0417-CONTROL/17TX1.Log"), "cal"),
        (os.path.join(USB_ROOT, "M13-LOGS/0417/20260417/17TX1.Log"), "ctrl"),
    ],
    "M14": [
        (os.path.join(USB_ROOT, "M14-LOGS/0417-CONTROL/16TX1.Log"), "cal"),
        (os.path.join(USB_ROOT, "M14-LOGS/0417/20260416/16TX1.Log"), "ctrl"),
        (os.path.join(USB_ROOT, "M14-LOGS/0417-CONTROL/17TX1.Log"), "cal"),
        (os.path.join(USB_ROOT, "M14-LOGS/0417/20260417/17TX1.Log"), "ctrl"),
    ],
}

PROD_DB = os.path.join(USB_ROOT, "M13M14-CAL-PC-LOG/drill_monitor.db")


# ── Parser ──

def parse_drive_log_full(log_path):
    """Parse a Drive.Log file in a single pass, returning hourly aggregation."""
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

        if bucket["prev_counter"] is not None:
            counter_delta = row["counter"] - bucket["prev_counter"]
            if counter_delta > 0:
                bucket["hole_count"] += counter_delta
        bucket["prev_counter"] = row["counter"]
        bucket["last_counter"] = row["counter"]

    return hourly, len(parsed_rows)


# ── Verification ──

def verify_utilization(machines, prod_db):
    """Compare full-parse results against production DB hourly data."""
    conn = sqlite3.connect(prod_db)
    conn.row_factory = sqlite3.Row

    print()
    print("=" * 90)
    print("  稼動率驗證：USB Drive.Log 獨立解析 vs Production DB")
    print("=" * 90)

    overall_pass = True

    for mid in machines:
        log_paths = LOG_FILES.get(mid, [])
        if not log_paths:
            continue

        # Parse all log files for this machine and merge hourly buckets
        merged = {}
        total_rows = 0
        for lp in log_paths:
            if not os.path.exists(lp):
                print("  [SKIP] 檔案不存在: {}".format(lp))
                continue
            hourly, row_count = parse_drive_log_full(lp)
            total_rows += row_count
            for key, bucket in hourly.items():
                if key not in merged:
                    merged[key] = {
                        "run": 0, "reset": 0, "stop": 0, "hole_count": 0,
                    }
                merged[key]["run"] += bucket["run"]
                merged[key]["reset"] += bucket["reset"]
                merged[key]["stop"] += bucket["stop"]
                merged[key]["hole_count"] += bucket["hole_count"]

        print()
        print("  {} — 解析 {:,} 列, {} 小時".format(mid, total_rows, len(merged)))
        print()
        print("  {:<12s} {:>4s}  {:>7s} {:>7s} {:>5s}  {:>7s} {:>7s} {:>5s}  "
              "{:>7s} {:>7s} {:>5s}  {:>8s} {:>8s} {:>6s}  {}".format(
                "日期", "時",
                "新RUN", "DB_RUN", "Δ",
                "新RST", "DB_RST", "Δ",
                "新STP", "DB_STP", "Δ",
                "新孔數", "DB孔數", "Δ",
                "結果"))
        print("  " + "-" * 120)

        diff_hours = 0
        for (date_str, hour) in sorted(merged.keys()):
            b = merged[(date_str, hour)]
            new_run, new_rst, new_stp = b["run"], b["reset"], b["stop"]
            new_holes = b["hole_count"]

            cursor = conn.execute(
                "SELECT run_seconds, reset_seconds, stop_seconds, hole_count "
                "FROM hourly_utilization WHERE machine_id=? AND date=? AND hour=?",
                (mid, date_str, hour),
            )
            db_row = cursor.fetchone()

            if db_row is None:
                print("  {:<12s} {:>4d}  {:>7d} {:>7s} {:>5s}  {:>7d} {:>7s} {:>5s}  "
                      "{:>7d} {:>7s} {:>5s}  {:>8,d} {:>8s} {:>6s}  {}".format(
                        date_str, hour,
                        new_run, "---", "", new_rst, "---", "",
                        new_stp, "---", "", new_holes, "---", "", "NO_DB"))
                diff_hours += 1
                continue

            db_run = db_row["run_seconds"]
            db_rst = db_row["reset_seconds"]
            db_stp = db_row["stop_seconds"]
            db_holes = db_row["hole_count"]

            d_run = new_run - db_run
            d_rst = new_rst - db_rst
            d_stp = new_stp - db_stp
            d_holes = new_holes - db_holes

            run_ok = abs(d_run) <= 2
            rst_ok = abs(d_rst) <= 2
            stp_ok = abs(d_stp) <= 2
            holes_ok = abs(d_holes) <= 5

            if run_ok and rst_ok and stp_ok and holes_ok:
                status = "PASS"
            else:
                status = "DIFF"
                diff_hours += 1

            def fmt_delta(d):
                if d == 0:
                    return ""
                return "{:+d}".format(d)

            print("  {:<12s} {:>4d}  {:>7d} {:>7d} {:>5s}  {:>7d} {:>7d} {:>5s}  "
                  "{:>7d} {:>7d} {:>5s}  {:>8,d} {:>8,d} {:>6s}  {}".format(
                    date_str, hour,
                    new_run, db_run, fmt_delta(d_run),
                    new_rst, db_rst, fmt_delta(d_rst),
                    new_stp, db_stp, fmt_delta(d_stp),
                    new_holes, db_holes, fmt_delta(d_holes),
                    status))

        # Daily summary
        print()
        print("  {} — 每日摘要:".format(mid))
        dates = sorted(set(d for d, h in merged.keys()))
        print("  {:<12s}  {:>7s}  {:>7s}  {:>7s}  {:>7s}  {:>6s}  {:>10s}  {:>10s}".format(
            "日期", "RUN(s)", "RST(s)", "STP(s)", "合計(s)", "稼動%", "新孔數", "DB孔數"))
        print("  " + "-" * 85)

        for d in dates:
            day_keys = [(d, h) for (dd, h) in merged if dd == d]
            run_sum = sum(merged[(d, h)]["run"] for _, h in day_keys)
            rst_sum = sum(merged[(d, h)]["reset"] for _, h in day_keys)
            stp_sum = sum(merged[(d, h)]["stop"] for _, h in day_keys)
            holes_sum = sum(merged[(d, h)]["hole_count"] for _, h in day_keys)
            total = run_sum + rst_sum + stp_sum
            util = (run_sum / total * 100.0) if total > 0 else 0.0

            cursor = conn.execute(
                "SELECT SUM(hole_count) as holes FROM hourly_utilization "
                "WHERE machine_id=? AND date=?", (mid, d))
            db_day = cursor.fetchone()
            db_holes_day = db_day["holes"] if db_day and db_day["holes"] else 0

            print("  {:<12s}  {:>7,d}  {:>7,d}  {:>7,d}  {:>7,d}  {:>5.1f}%  {:>10,d}  {:>10,d}".format(
                d, run_sum, rst_sum, stp_sum, total, util, holes_sum, db_holes_day))

        if diff_hours > 0:
            overall_pass = False
            print()
            print("  [DIFF] {} 有 {} 小時有差異（容許值：時間 ±2s, 孔數 ±5）".format(mid, diff_hours))
        else:
            print()
            print("  [PASS] {} 全部小時與 Production DB 一致".format(mid))

    conn.close()
    return overall_pass


def verify_work_orders(machines):
    """Show all work order events from TX1.Log files."""
    print()
    print("=" * 90)
    print("  工單號驗證：TX1.Log FILEOPERATION LOAD 事件")
    print("=" * 90)

    for mid in machines:
        tx1_paths = TX1_FILES.get(mid, [])
        if not tx1_paths:
            continue

        print()
        print("  {} — TX1.Log 工單事件:".format(mid))

        for log_path, source in tx1_paths:
            if not os.path.exists(log_path):
                continue

            events = []
            try:
                with open(log_path, "r", encoding="cp932", errors="replace") as f:
                    for line in f:
                        m = FILEOPERATION_LOAD_PATTERN.match(line.strip())
                        if m:
                            program = m.group(2).strip()
                            wo, side = extract_work_order(program)
                            if wo:
                                ts = m.group(1).replace("/", "-", 2).replace(" ", "T", 1)
                                events.append((ts, program, wo, side))
            except Exception as e:
                print("    [ERROR] {}: {}".format(log_path, e))
                continue

            basename = os.path.basename(log_path)
            if events:
                print()
                print("    {} ({}, {} 筆工單 LOAD):".format(basename, source, len(events)))
                for ts, prog, wo, side in events:
                    print("      {}  {:<20s}  -> {}.{}".format(ts, prog, wo, side))
            else:
                lines = 0
                try:
                    with open(log_path, "r", encoding="cp932", errors="replace") as f:
                        lines = sum(1 for _ in f)
                except Exception:
                    pass
                print("    {} ({}, {} 行, 無工單 LOAD 事件)".format(basename, source, lines))

    # Compare with production DB
    if os.path.exists(PROD_DB):
        conn = sqlite3.connect(PROD_DB)
        conn.row_factory = sqlite3.Row
        print()
        print("  Production DB machine_current_state:")
        cursor = conn.execute(
            "SELECT machine_id, work_order, work_order_side, last_update "
            "FROM machine_current_state ORDER BY machine_id")
        for row in cursor.fetchall():
            print("    {}: {}.{} (last_update={})".format(
                row["machine_id"], row["work_order"], row["work_order_side"],
                row["last_update"]))
        conn.close()


def main():
    print("=" * 90)
    print("  USB LOG vs Production DB 交叉驗證報告")
    print("  Production DB: {}".format(PROD_DB))
    print("=" * 90)

    if not os.path.exists(USB_ROOT):
        print("\n  [ERROR] USB 未掛載: {}".format(USB_ROOT))
        sys.exit(1)

    if not os.path.exists(PROD_DB):
        print("\n  [ERROR] Production DB 不存在: {}".format(PROD_DB))
        sys.exit(1)

    machines = ["M13", "M14"]
    util_pass = verify_utilization(machines, PROD_DB)
    verify_work_orders(machines)

    print()
    print("=" * 90)
    if util_pass:
        print("  結論: 稼動率 & 孔數驗證通過 — 數據可信")
    else:
        print("  結論: 有差異項目需要檢查（見 DIFF）")
    print("=" * 90)


if __name__ == "__main__":
    main()

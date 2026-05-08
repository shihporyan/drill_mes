"""Check laser (L1-L4) work-order naming compliance for a given month.

Spec: {WD|GR}-{工單號}-{TOP|BOT}-{板別}-{補孔累計}-{重工累計}
      e.g. WD-2604101-TOP-A-0-0   (6 dash-separated fields)

Usage:
    python3 tools/check_laser_wo_naming.py 05
    python3 tools/check_laser_wo_naming.py 05 --db drill_monitor.db
    python3 tools/check_laser_wo_naming.py 05 --year 2026

Filters: machines L1-L4 only, work_order starting with WD or GR.
Output : work_order | start_time | end_time | result | reason
"""

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "drill_monitor.db")

VALID_FIXTURE = {"TOP", "BOT"}
RE_WO_NUM = re.compile(r"^\d+$")
RE_BOARD = re.compile(r"^[A-Z]$")
RE_DIGITS = re.compile(r"^\d+$")


def validate(wo: str):
    """Return (ok: bool, reason: str)."""
    if "_" in wo:
        return False, "含底線（規範用 '-' 分隔）"
    parts = wo.split("-")
    # WD-XXXXXXX-TOP-A-0-0  →  6 parts
    if len(parts) != 6:
        return False, f"欄位數={len(parts)}，應為 6（WD-工單-治具-板-補孔-重工）"
    p0, wo_num, fixture, board, repair, rework = parts
    if p0 not in ("WD", "GR"):
        return False, f"開頭不是 WD/GR（'{p0}'）"
    if not RE_WO_NUM.match(wo_num):
        return False, f"工單號 '{wo_num}' 應為純數字"
    if fixture not in VALID_FIXTURE:
        return False, f"治具 '{fixture}' 應為 TOP 或 BOT"
    if not RE_BOARD.match(board):
        return False, f"板別 '{board}' 應為單一大寫英文字母"
    if not RE_DIGITS.match(repair):
        return False, f"補孔累計 '{repair}' 應為數字"
    if not RE_DIGITS.match(rework):
        return False, f"重工累計 '{rework}' 應為數字"
    return True, "OK"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("month", help="月份，例如 05")
    ap.add_argument("--year", default=str(datetime.now().year),
                    help="年份，預設今年")
    ap.add_argument("--db", default=DEFAULT_DB,
                    help=f"DB 路徑（預設: {DEFAULT_DB}）")
    args = ap.parse_args()

    month = args.month.zfill(2)
    year = args.year
    prefix = f"{year}-{month}"

    db_path = os.path.abspath(args.db)
    if not os.path.isfile(db_path):
        print(f"ERROR: DB 檔不存在: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='laser_work_orders'"
    )
    if not cur.fetchone():
        print(f"ERROR: DB 內找不到 laser_work_orders 表（{db_path}）", file=sys.stderr)
        print("  → 請確認 monitor service 已啟動並完成 init_db migration。", file=sys.stderr)
        return 3

    cur.execute(
        "SELECT machine_id, work_order, start_time, end_time "
        "FROM laser_work_orders "
        "WHERE machine_id IN ('L1','L2','L3','L4') "
        "  AND (work_order LIKE 'WD%' OR work_order LIKE 'GR%') "
        "  AND substr(start_time,1,7) = ? "
        "ORDER BY machine_id, start_time",
        (prefix,),
    )
    rows = cur.fetchall()

    if not rows:
        print(f"(no WD work orders for L1-L4 in {prefix})")
        return 0

    # Pretty table
    headers = ["機台", "工號名", "加工開始時間", "加工結束時間", "判斷結果", "理由"]
    out = []
    pass_n = fail_n = 0
    for machine_id, wo, start, end in rows:
        ok, reason = validate(wo)
        if ok:
            pass_n += 1
        else:
            fail_n += 1
        out.append([
            machine_id,
            wo,
            start or "",
            end or "",
            "PASS" if ok else "FAIL",
            reason,
        ])

    widths = [max(len(str(r[i])) for r in [headers] + out) for i in range(len(headers))]
    fmt = " | ".join("{:<" + str(w) + "}" for w in widths)
    print(fmt.format(*headers))
    print("-+-".join("-" * w for w in widths))
    for r in out:
        print(fmt.format(*r))

    print()
    print(f"總計: {len(rows)}  PASS: {pass_n}  FAIL: {fail_n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

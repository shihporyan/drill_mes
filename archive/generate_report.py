"""
Takeuchi 鑽孔機 Log 分析驗證報告 — PDF 生成腳本
目的：讓有經驗的維修人員確認我們的 log 處理方式是否正確
"""

import os
import re
import tempfile
from collections import defaultdict
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

from fpdf import FPDF

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MACHINE_ID = "DRILL-01"
DATA_DATE = "20260317"
DAY_PREFIX = "17"
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs", MACHINE_ID, DATA_DATE)
OUTPUT_PDF = os.path.join(os.path.dirname(__file__), f"report_{MACHINE_ID.lower()}_{DATA_DATE}.pdf")

FONT_PATH = "C:/Windows/Fonts/msjh.ttc"  # 微軟正黑體
FONT_INDEX = 0

# ---------------------------------------------------------------------------
# Log Parsers
# ---------------------------------------------------------------------------

def parse_drive_log(path):
    """Parse Drive.Log — returns hourly stats and state transitions."""
    hourly = defaultdict(lambda: {"RUN": 0, "RESET": 0, "STOP": 0, "total": 0})
    transitions = []
    prev_status = None
    sample_lines = {"RUN": None, "RESET": None, "STOP": None}

    with open(path, "r", encoding="ascii", errors="replace") as f:
        for line in f:
            parts = line.strip().split(",")
            if len(parts) < 10:
                continue
            date_str = parts[0]       # 2026/03/17
            time_str = parts[1]       # 00:00:00
            mode = parts[2]           # AUTO / MAN
            status = parts[3]         # RUN / RESET / STOP
            program = parts[4]        # O100.txt
            tool_no = parts[7].strip() if len(parts) > 7 else ""
            drill_dia = parts[8].strip() if len(parts) > 8 else ""

            hour = int(time_str.split(":")[0])
            hourly[hour][status] += 1
            hourly[hour]["total"] += 1

            if status != prev_status:
                transitions.append({
                    "date": date_str,
                    "time": time_str,
                    "from": prev_status or "N/A",
                    "to": status,
                    "program": program,
                    "tool": tool_no,
                    "dia": drill_dia,
                })
                prev_status = status

            # Capture sample lines
            if sample_lines.get(status) is None:
                sample_lines[status] = line.strip()[:120]

    return hourly, transitions, sample_lines


def parse_tarn_log(path):
    """Parse TARN.Log — returns key events (start/stop/reset/tool change)."""
    events = []
    with open(path, "rb") as f:
        data = f.read()
    text = data.decode("cp932", errors="replace")

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        # Events: 起動, 停止, リセット, 異常リセット, ToolChenge
        event_type = None
        if "起動" in line and "MB300700" in line:
            event_type = "起動 (Start)"
        elif "停止" in line and "MB300721" in line:
            event_type = "停止 (Stop)"
        elif "異常リセット" in line and "MB300940" in line:
            event_type = "異常重置 (Error Reset)"
        elif "リセット" in line and "MB300740" in line:
            event_type = "重置 (Reset)"
        elif "ToolChenge" in line and "ST:" in line:
            # Extract tool seat and block
            m = re.search(r"ToolChenge.*?ST:\[(\d+)\]\s*BLOCK:\[(\d+)\]", line)
            if m:
                event_type = f"換刀 ST:{m.group(1)} BLK:{m.group(2)}"

        if event_type:
            # Extract timestamp
            ts_match = re.match(r"(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)", line)
            ts = ts_match.group(1) if ts_match else ""
            events.append({
                "timestamp": ts,
                "type": event_type,
                "raw": line[:100],
            })

    return events


def parse_tx1_log(path):
    """Parse TX1.Log — returns LoadProgram events and operator actions."""
    load_events = []
    button_events = []

    with open(path, "rb") as f:
        data = f.read()
    text = data.decode("cp932", errors="replace")

    for line in text.splitlines():
        line = line.strip()
        if "LoadProgram" in line and "ReadProgram" in line:
            m = re.search(r"LoadProgram\((.+?)\)", line)
            prog = m.group(1).strip() if m else "?"
            prog_name = os.path.basename(prog)
            ts_match = re.match(r"(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)", line)
            ts = ts_match.group(1) if ts_match else ""
            load_events.append({"timestamp": ts, "program": prog_name, "path": prog})
        elif "BUTTON PUSH" in line:
            m_btn = re.search(r"BUTTON:\[(.+?)\]", line)
            m_scr = re.search(r"SCREEN:\[(.+?)\]", line)
            ts_match = re.match(r"(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)", line)
            ts = ts_match.group(1) if ts_match else ""
            button_events.append({
                "timestamp": ts,
                "button": m_btn.group(1) if m_btn else "?",
                "screen": m_scr.group(1) if m_scr else "?",
            })

    return load_events, button_events


def parse_alarm_log(path):
    """Parse Alarm.Log — returns alarm events."""
    alarms = []
    with open(path, "r", encoding="ascii", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                parts = line.split(",")
                alarms.append({
                    "date": parts[0] if parts else "",
                    "time": parts[1] if len(parts) > 1 else "",
                    "code": parts[2] if len(parts) > 2 else "",
                    "raw": line[:100],
                })
    return alarms


# ---------------------------------------------------------------------------
# Chart Generation
# ---------------------------------------------------------------------------

def build_utilization_chart(hourly, output_path):
    """Create hourly utilization bar chart using matplotlib."""
    # Configure CJK font
    font_prop = font_manager.FontProperties(fname=FONT_PATH)
    plt.rcParams["font.family"] = font_prop.get_name()
    plt.rcParams["axes.unicode_minus"] = False

    hours_with_data = sorted(h for h in hourly if hourly[h]["total"] > 0)
    if not hours_with_data:
        return

    hours = list(range(hours_with_data[0], hours_with_data[-1] + 1))
    util_rates = []
    run_seconds = []
    for h in hours:
        total = hourly[h]["total"]
        run = hourly[h]["RUN"]
        rate = (run / total * 100) if total > 0 else 0
        util_rates.append(rate)
        run_seconds.append(run)

    fig, ax1 = plt.subplots(figsize=(10, 4.5))

    colors = ["#2196F3" if r > 50 else "#FF9800" if r > 20 else "#E0E0E0" for r in util_rates]
    bars = ax1.bar(hours, util_rates, color=colors, edgecolor="#333", linewidth=0.5)

    # Add value labels on bars
    for bar, rate, run in zip(bars, util_rates, run_seconds):
        if rate > 0:
            ax1.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1.5,
                     f"{rate:.0f}%", ha="center", va="bottom", fontsize=8,
                     fontproperties=font_prop)

    ax1.set_xlabel("Hour", fontproperties=font_prop, fontsize=11)
    ax1.set_ylabel("Utilization Rate (%)", fontproperties=font_prop, fontsize=11)
    ax1.set_title(f"DRILL-01  Hourly Utilization  ({DATA_DATE[:4]}/{DATA_DATE[4:6]}/{DATA_DATE[6:]})",
                  fontproperties=font_prop, fontsize=13, fontweight="bold")
    ax1.set_xticks(hours)
    ax1.set_xticklabels([f"{h:02d}:00" for h in hours], rotation=45, ha="right", fontsize=8)
    ax1.set_ylim(0, 110)
    ax1.axhline(y=50, color="#999", linestyle="--", linewidth=0.8, alpha=0.5)

    # Legend
    from matplotlib.patches import Patch
    legend_elements = [
        Patch(facecolor="#2196F3", label=">50%"),
        Patch(facecolor="#FF9800", label="20-50%"),
        Patch(facecolor="#E0E0E0", label="<20%"),
    ]
    ax1.legend(handles=legend_elements, loc="upper right", prop=font_prop)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()


# ---------------------------------------------------------------------------
# PDF Report
# ---------------------------------------------------------------------------

class DrillReport(FPDF):
    """Custom PDF class for the drill analysis report."""

    def __init__(self):
        super().__init__(orientation="P", unit="mm", format="A4")
        # Register CJK font
        self.add_font("msjh", "", FONT_PATH)
        self.add_font("msjh", "B", FONT_PATH)  # Bold variant
        self.set_auto_page_break(auto=True, margin=20)

    def header(self):
        if self.page_no() == 1:
            return  # Skip header on cover page
        self.set_font("msjh", "B", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 6, f"DRILL-01 Log Analysis Report  |  Data: {DATA_DATE}", align="R")
        self.ln(8)
        self.set_draw_color(200, 200, 200)
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(3)

    def footer(self):
        self.set_y(-15)
        self.set_font("msjh", "", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"- {self.page_no()} -", align="C")

    # --- Helpers ---

    def cover_page(self):
        self.add_page()
        self.ln(50)
        self.set_font("msjh", "B", 26)
        self.set_text_color(33, 33, 33)
        self.cell(0, 15, "Takeuchi \u9452\u5b54\u6a5f", align="C", new_x="LMARGIN", new_y="NEXT")
        self.cell(0, 15, "Log \u5206\u6790\u9a57\u8b49\u5831\u544a", align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(15)
        self.set_font("msjh", "", 14)
        self.set_text_color(80, 80, 80)
        info_lines = [
            f"\u6a5f\u53f0\u7de8\u865f\uff1a{MACHINE_ID}",
            f"\u6578\u64da\u65e5\u671f\uff1a{DATA_DATE[:4]}/{DATA_DATE[4:6]}/{DATA_DATE[6:]}",
            f"\u5831\u544a\u65e5\u671f\uff1a{datetime.now().strftime('%Y/%m/%d')}",
            "",
            "\u76ee\u7684\uff1a\u8acb\u6709\u7d93\u9a57\u7684\u7dad\u4fee\u4eba\u54e1\u78ba\u8a8d",
            "\u6211\u5011\u5c0d Log \u6578\u64da\u7684\u5224\u5b9a\u65b9\u5f0f\u662f\u5426\u6b63\u78ba",
        ]
        for line in info_lines:
            self.cell(0, 10, line, align="C", new_x="LMARGIN", new_y="NEXT")

    def chapter_title(self, num, title):
        self.set_font("msjh", "B", 16)
        self.set_text_color(25, 118, 210)
        self.ln(5)
        self.cell(0, 12, f"{num}. {title}", new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(25, 118, 210)
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(5)

    def section_title(self, title):
        self.set_font("msjh", "B", 12)
        self.set_text_color(33, 33, 33)
        self.cell(0, 9, title, new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def body_text(self, text):
        self.set_font("msjh", "", 10)
        self.set_text_color(50, 50, 50)
        self.multi_cell(0, 6, text)
        self.ln(2)

    def raw_log_block(self, text, max_lines=8):
        """Display raw log content in a shaded monospace block."""
        self.set_fill_color(245, 245, 245)
        self.set_draw_color(200, 200, 200)
        self.set_font("msjh", "", 7)
        self.set_text_color(60, 60, 60)

        lines = text.strip().split("\n")[:max_lines]
        x = self.get_x()
        y = self.get_y()
        block_h = len(lines) * 4.5 + 4

        # Check page break
        if y + block_h > 270:
            self.add_page()
            y = self.get_y()

        self.rect(10, y, 190, block_h, style="DF")
        self.set_xy(12, y + 2)
        for line in lines:
            self.cell(0, 4.5, line[:130], new_x="LMARGIN", new_y="NEXT")
            self.set_x(12)
        self.ln(3)

    def add_table(self, headers, rows, col_widths=None):
        """Add a formatted table."""
        if col_widths is None:
            col_widths = [190 / len(headers)] * len(headers)

        # Check page break for header + at least 2 rows
        needed = 8 + min(len(rows), 2) * 7
        if self.get_y() + needed > 270:
            self.add_page()

        # Header
        self.set_font("msjh", "B", 9)
        self.set_fill_color(25, 118, 210)
        self.set_text_color(255, 255, 255)
        for i, h in enumerate(headers):
            self.cell(col_widths[i], 8, h, border=1, fill=True, align="C")
        self.ln()

        # Rows
        self.set_font("msjh", "", 9)
        self.set_text_color(50, 50, 50)
        for row_idx, row in enumerate(rows):
            if self.get_y() + 7 > 270:
                self.add_page()
                # Re-draw header
                self.set_font("msjh", "B", 9)
                self.set_fill_color(25, 118, 210)
                self.set_text_color(255, 255, 255)
                for i, h in enumerate(headers):
                    self.cell(col_widths[i], 8, h, border=1, fill=True, align="C")
                self.ln()
                self.set_font("msjh", "", 9)
                self.set_text_color(50, 50, 50)

            if row_idx % 2 == 0:
                self.set_fill_color(240, 247, 255)
            else:
                self.set_fill_color(255, 255, 255)
            for i, cell in enumerate(row):
                self.cell(col_widths[i], 7, str(cell), border=1, fill=True, align="C")
            self.ln()
        self.ln(3)


def generate_pdf(hourly, transitions, sample_lines, tarn_events, load_events,
                 button_events, alarms, chart_path):
    """Assemble the full PDF report."""
    pdf = DrillReport()

    # ===== Cover Page =====
    pdf.cover_page()

    # ===== Chapter 1: What We Can Determine =====
    pdf.add_page()
    pdf.chapter_title("一", "我們能從 Log 判定什麼")

    pdf.body_text(
        "透過分析鑽孔機每日自動產生的 6 個 Log 檔案，我們可以判定以下 6 項關鍵指標。"
        "所有數據皆為機器自動記錄，無人工介入，可作為客觀依據。"
    )

    indicators = [
        ["機台開機 / 停機時間", "TARN.Log (起動/停止)", "毫秒級", "機器何時開始鑽孔、何時停止"],
        ["稼動率 (每小時/每日)", "Drive.Log (RUN/RESET/STOP)", "秒級", "實際鑽孔時間佔總時間比例"],
        ["停機時段與持續時間", "Drive.Log (狀態變化)", "秒級", "哪些時段機器閒置、閒置多久"],
        ["程式載入事件與時間", "TX1.Log (LoadProgram)", "秒級", "何時載入哪個 NC 程式"],
        ["換刀事件與刀具序列", "TARN.Log (ToolChenge)", "毫秒級", "換刀順序、刀座號、區塊號"],
        ["報警 / 異常事件", "Alarm.Log + TARN.Log", "秒/毫秒", "報警代碼與異常重置時間"],
    ]

    pdf.add_table(
        ["判定指標", "資料來源", "精度", "說明"],
        indicators,
        col_widths=[45, 40, 20, 85],
    )

    # ===== Chapter 2: Data Sources & Methodology =====
    pdf.add_page()
    pdf.chapter_title("二", "資料來源與判定依據")

    # 2.1 Drive.Log
    pdf.section_title("2.1  Drive.Log — 稼動率核心依據")
    pdf.body_text(
        "Drive.Log 為逐秒 CSV 記錄，每行包含：日期、時間、模式、狀態、程式名、"
        "座標、刀具號、針徑等欄位。我們使用第 4 欄「狀態」(RUN / RESET / STOP) "
        "來計算稼動率。"
    )
    pdf.body_text("判定邏輯：統計每小時 RUN 秒數 ÷ 該小時總秒數 = 稼動率。")
    pdf.body_text("原始格式範例 (各狀態各一行)：")
    raw_lines = []
    for status in ["RESET", "RUN", "STOP"]:
        if sample_lines.get(status):
            raw_lines.append(f"[{status}] {sample_lines[status]}")
    pdf.raw_log_block("\n".join(raw_lines))

    # 2.2 TARN.Log
    pdf.section_title("2.2  TARN.Log — 精確事件時間戳")
    pdf.body_text(
        "TARN.Log 記錄離散事件（起動、停止、重置、換刀），時間精確到毫秒。"
        "我們使用 MB 碼識別事件類型："
    )
    pdf.add_table(
        ["事件", "日文關鍵字", "MB 碼", "意義"],
        [
            ["起動", "起動", "MB300700", "機台開始鑽孔"],
            ["停止", "停止", "MB300721", "機台停止鑽孔"],
            ["重置", "リセット", "MB300740", "機台重置狀態"],
            ["異常重置", "異常リセット", "MB300940", "異常後重置"],
            ["換刀", "ToolChenge", "—", "刀座號 + 區塊號"],
        ],
        col_widths=[30, 35, 35, 90],
    )
    pdf.body_text("原始格式範例：")
    tarn_samples = [e["raw"] for e in tarn_events[:5]]
    pdf.raw_log_block("\n".join(tarn_samples))

    # 2.3 TX1.Log
    pdf.section_title("2.3  TX1.Log — 程式載入記錄")
    pdf.body_text(
        "TX1.Log 記錄操作員動作，包括 LoadProgram（載入程式）、BUTTON PUSH（按鍵操作）"
        "等。我們擷取 LoadProgram 事件來追蹤何時載入了哪個 NC 程式。"
    )
    pdf.body_text("原始格式範例：")
    tx1_samples = []
    for e in load_events[:3]:
        tx1_samples.append(f"{e['timestamp']}  LoadProgram → {e['program']}")
    pdf.raw_log_block("\n".join(tx1_samples))

    # 2.4 Alarm.Log
    pdf.section_title("2.4  Alarm.Log — 報警記錄")
    pdf.body_text(
        "Alarm.Log 記錄報警代碼和時間。欄位為：日期、時間、報警碼。"
    )
    if alarms:
        alarm_samples = [a["raw"] for a in alarms[:5]]
        pdf.raw_log_block("\n".join(alarm_samples))
    else:
        pdf.body_text("(當日無報警記錄)")

    # 2.5 Cross-validation
    pdf.section_title("2.5  交叉驗證")
    pdf.body_text(
        "TARN.Log 提供毫秒級精確事件（適合判定開始/結束時間），"
        "Drive.Log 提供逐秒連續狀態（適合計算稼動率）。"
        "兩者可互相驗證：TARN.Log 的「起動」事件時間應與 Drive.Log 中"
        "狀態從 RESET/STOP 變為 RUN 的時間吻合（誤差在 1 秒以內）。"
    )

    # ===== Chapter 3: Real Data Examples =====
    pdf.add_page()
    pdf.chapter_title("三", "實際數據範例 (2026/03/17)")

    # Example 1: Utilization Rate
    pdf.section_title("範例 1：稼動率計算")
    pdf.body_text(
        "以下為 2026/03/17 各小時稼動率統計。"
        "稼動率 = 該小時 RUN 秒數 ÷ 該小時總記錄秒數 × 100%。"
    )

    hours_sorted = sorted(hourly.keys())
    total_run = sum(hourly[h]["RUN"] for h in hours_sorted)
    total_all = sum(hourly[h]["total"] for h in hours_sorted)

    util_rows = []
    for h in hours_sorted:
        if hourly[h]["total"] == 0:
            continue
        run = hourly[h]["RUN"]
        reset = hourly[h]["RESET"]
        stop = hourly[h]["STOP"]
        total = hourly[h]["total"]
        rate = run / total * 100 if total > 0 else 0
        util_rows.append([
            f"{h:02d}:00",
            f"{run:,}",
            f"{reset:,}",
            f"{stop:,}",
            f"{total:,}",
            f"{rate:.1f}%",
        ])
    overall_rate = total_run / total_all * 100 if total_all > 0 else 0
    util_rows.append([
        "整日合計",
        f"{total_run:,}",
        "—",
        "—",
        f"{total_all:,}",
        f"{overall_rate:.1f}%",
    ])

    pdf.add_table(
        ["時段", "RUN (秒)", "RESET (秒)", "STOP (秒)", "總計 (秒)", "稼動率"],
        util_rows,
        col_widths=[25, 28, 28, 28, 28, 25],
    )

    pdf.body_text("稼動率分佈圖：")
    if os.path.exists(chart_path):
        img_w = 180
        if pdf.get_y() + 80 > 270:
            pdf.add_page()
        pdf.image(chart_path, x=15, w=img_w)
        pdf.ln(5)

    # Example 2: Start/Stop Times from TARN.Log
    pdf.add_page()
    pdf.section_title("範例 2：開機 / 停機時間判定 (TARN.Log)")
    pdf.body_text(
        "以下為從 TARN.Log 擷取的起動/停止/重置事件，精確到毫秒。"
        "維修人員可對照機台操作記錄確認時間是否正確。"
    )

    start_stop_events = [e for e in tarn_events
                         if any(k in e["type"] for k in ["起動", "停止", "重置"])]
    ss_rows = []
    for e in start_stop_events:
        ts = e["timestamp"]
        time_part = ts.split(" ")[1] if " " in ts else ts
        ss_rows.append([time_part, e["type"]])

    pdf.add_table(
        ["時間 (毫秒精度)", "事件類型"],
        ss_rows,
        col_widths=[70, 120],
    )

    # Example 3: Program Loading (TX1.Log)
    pdf.section_title("範例 3：程式載入追蹤 (TX1.Log)")
    pdf.body_text(
        "以下為操作員載入 NC 程式的記錄。可判定何時切換了工單/程式。"
    )
    load_rows = []
    for e in load_events:
        ts = e["timestamp"]
        time_part = ts.split(" ")[1] if " " in ts else ts
        load_rows.append([time_part, e["program"], e["path"]])

    pdf.add_table(
        ["時間", "程式名", "完整路徑"],
        load_rows,
        col_widths=[35, 40, 115],
    )

    # Example 4: Tool Changes (TARN.Log)
    pdf.section_title("範例 4：換刀事件追蹤 (TARN.Log)")
    pdf.body_text(
        "以下為換刀事件摘錄（前 20 筆）。ST = 刀座號, BLK = 區塊號。"
    )
    tool_events = [e for e in tarn_events if "換刀" in e["type"]]
    tc_rows = []
    for e in tool_events[:20]:
        ts = e["timestamp"]
        time_part = ts.split(" ")[1] if " " in ts else ts
        tc_rows.append([time_part, e["type"]])

    pdf.add_table(
        ["時間", "換刀資訊"],
        tc_rows,
        col_widths=[60, 130],
    )

    # Example 5: Downtime Analysis
    pdf.add_page()
    pdf.section_title("範例 5：停機時段分析 (Drive.Log)")
    pdf.body_text(
        "以下為 Drive.Log 中偵測到的所有狀態變化（共 "
        f"{len(transitions)} 次）。可用來識別停機時段及持續時間。"
    )

    # Show meaningful transitions (filtering very short ones)
    dt_rows = []
    for i, t in enumerate(transitions):
        dt_rows.append([
            t["time"],
            t["from"],
            t["to"],
            t["program"],
        ])

    pdf.add_table(
        ["時間", "從", "到", "程式"],
        dt_rows,
        col_widths=[35, 30, 30, 95],
    )

    # ===== Chapter 4: Conclusion =====
    pdf.add_page()
    pdf.chapter_title("四", "結論與確認請求")

    pdf.section_title("4.1  判定方式摘要")
    pdf.body_text(
        "1. 稼動率：以 Drive.Log 每秒狀態為準，統計 RUN 秒數佔比。\n"
        "2. 開停機時間：以 TARN.Log 的起動/停止事件為準（毫秒精度）。\n"
        "3. 停機時段：以 Drive.Log 狀態從 RUN 變為 RESET/STOP 的時間點為準。\n"
        "4. 程式切換：以 TX1.Log 的 LoadProgram 事件為準。\n"
        "5. 換刀序列：以 TARN.Log 的 ToolChenge 事件為準。\n"
        "6. 報警：以 Alarm.Log + TARN.Log 異常重置為準。"
    )

    pdf.section_title("4.2  請確認以下問題")
    confirm_questions = [
        "Drive.Log 的 RUN 狀態是否等同於「正在鑽孔」？\n"
        "（或是否有其他 RUN 但非鑽孔的情況？）",

        "TARN.Log 的「起動」(MB300700) 事件是否代表操作員按下啟動？\n"
        "時間是否與您的操作記憶吻合？",

        "開機前的反覆 RUN→STOP 短週期（00:05~00:07），\n"
        "是否為調參試鑽？這段時間是否應計入稼動率？",

        "07:55 重新起動後到 08:05 停止，是否為早班開始前的暖機/試鑽？",

        "10:49 載入 O2603044.B 後到 10:56 才正式 RUN，\n"
        "中間的等待時間通常用於什麼操作？",

        "Alarm 代碼 6802 (08:10:59) 和 1047 (10:50~10:52)\n"
        "分別代表什麼問題？是否影響後續判斷？",

        "整日稼動率 17.6% 包含深夜閒置時段。\n"
        "實際考核是否只看排班時段（如 08:00~17:00）？",
    ]

    for i, q in enumerate(confirm_questions, 1):
        pdf.set_font("msjh", "B", 10)
        pdf.set_text_color(33, 33, 33)
        pdf.cell(0, 7, f"Q{i}:", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("msjh", "", 10)
        pdf.set_text_color(50, 50, 50)
        pdf.multi_cell(0, 6, q)
        # Checkbox placeholder
        pdf.set_font("msjh", "", 10)
        pdf.set_text_color(25, 118, 210)
        y_check = pdf.get_y()
        pdf.cell(0, 6, "□ 正確    □ 需修正：_______________", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

    # Save
    pdf.output(OUTPUT_PDF)
    return OUTPUT_PDF


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"Parsing logs from: {LOG_DIR}")

    drive_path = os.path.join(LOG_DIR, f"{DAY_PREFIX}Drive.Log")
    tarn_path = os.path.join(LOG_DIR, f"{DAY_PREFIX}TARN.Log")
    tx1_path = os.path.join(LOG_DIR, f"{DAY_PREFIX}TX1.Log")
    alarm_path = os.path.join(LOG_DIR, f"{DAY_PREFIX}Alarm.Log")

    # Parse logs
    print("  Parsing Drive.Log...")
    hourly, transitions, sample_lines = parse_drive_log(drive_path)
    print(f"    {len(transitions)} state transitions found")

    print("  Parsing TARN.Log...")
    tarn_events = parse_tarn_log(tarn_path)
    print(f"    {len(tarn_events)} events found")

    print("  Parsing TX1.Log...")
    load_events, button_events = parse_tx1_log(tx1_path)
    print(f"    {len(load_events)} LoadProgram, {len(button_events)} button events")

    print("  Parsing Alarm.Log...")
    alarms = parse_alarm_log(alarm_path)
    print(f"    {len(alarms)} alarm records")

    # Generate chart
    chart_path = os.path.join(tempfile.gettempdir(), "drill_utilization_chart.png")
    print("  Generating utilization chart...")
    build_utilization_chart(hourly, chart_path)

    # Generate PDF
    print("  Generating PDF report...")
    output = generate_pdf(hourly, transitions, sample_lines, tarn_events,
                          load_events, button_events, alarms, chart_path)
    print(f"\nReport generated: {output}")
    print(f"File size: {os.path.getsize(output):,} bytes")

    # Cleanup
    if os.path.exists(chart_path):
        os.remove(chart_path)


if __name__ == "__main__":
    main()

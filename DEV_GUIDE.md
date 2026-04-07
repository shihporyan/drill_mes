# 鑽孔機生產資訊 E 化 — 開發指引

> **用途**：交付給 VS Code Claude Code 進行第一次開發
> **版本**：v1.0（2026/04/06）
> **專案名稱**：鑽孔機生產資訊E化（drill-monitoring）
> **負責人**：Ryan @ 睿智精密科技

---

## 1. 專案概述

### 目標
取代人工 USB 收集 LOG 的流程，建立自動化即時監控系統：
- 從 Takeuchi 鑽孔機的 Drive.Log 自動解析稼動率（utilization）和孔數
- 存入 SQLite 資料庫
- 透過 HTTP API 供 Dashboard 前端使用
- 未來擴展至 4 台 Kataoka 雷射鑽孔機（LOG 格式不同，待調查）

### 技術限制（嚴格遵守）
- **Python 3.x，stdlib only** — 不使用任何第三方套件（no pip install）
- **運行環境**：Windows 10 運算主機（1F 現場），全地端無網路
- **資料庫**：SQLite（stdlib sqlite3 模組）
- **HTTP server**：stdlib http.server
- **前端**：單檔 HTML（React 18 + Babel via CDN）
- **安全要求**：HTTP server 必須綁定 office NIC IP，禁止綁定 `0.0.0.0`
- **IP forwarding 必須關閉**（資安規定）

### 當前狀態（初版開發限制）
- **只有 M13 和 M14 兩台控制電腦已連線**，其餘 16 台尚未完成網路部署
- 程式必須能在只有 2 台的情況下正常運行，並在更多機台連線後無縫擴展
- `net use` SMB 連線指令尚未執行（等全部線路安裝好後一次處理）
- 4 台 Kataoka 雷射鑽孔機的 LOG 格式尚未確認，架構需保持擴展彈性

---

## 2. 系統架構

```
┌─────────────────────────────────────────────────────────────┐
│  18 台 Takeuchi 控制電腦（10.10.1.11 ~ 10.10.1.28）          │
│  每台透過 SMB 共享 \\{ip}\LOG\ 目錄                          │
└──────────────────────┬──────────────────────────────────────┘
                       │ robocopy（每 10 分鐘）
                       ▼
┌──────────────────────────────────────────────────────────────┐
│  運算主機（10.10.1.2，Win10，1F 現場）                        │
│                                                              │
│  C:\DrillLogs\M13\YYYYMMDD\DDDrive.Log  ← 本地備份          │
│         │                                                    │
│         ▼                                                    │
│  drive_log_parser.py  → drill_monitor.db（SQLite）           │
│         │                                                    │
│         ▼                                                    │
│  api_server.py（HTTP，綁 office NIC IP:8080）                │
│         │                                                    │
│         ▼                                                    │
│  dashboard.html（React + Babel CDN，單檔）                   │
└──────────────────────────────────────────────────────────────┘
                       │ 未來 Phase F
                       ▼
              NAS → MES 主機（2F）
```

### 網路架構
- **雙 NIC 隔離**：每台控制電腦有兩張網卡
  - NIC1（內建）：`192.168.1.2` 連鑽孔機控制器（絕對不動）
  - NIC2（USB/PCI/內建第二口）：`10.10.1.XX` 連監控網路
- 運算主機：`10.10.1.2`
- 控制電腦 IP 對照：

| 機台 | IP | 備註 |
|------|-----|------|
| M01 | 10.10.1.11 | |
| M02 | 10.10.1.12 | |
| M03 | 10.10.1.13 | |
| M04 | 10.10.1.14 | |
| M05 | 10.10.1.15 | |
| M06 | 10.10.1.16 | |
| M07 | 10.10.1.17 | |
| M08 | 10.10.1.18 | |
| M09 | 10.10.1.19 | |
| M10 | 10.10.1.20 | |
| M11 | 10.10.1.21 | |
| M12 | 10.10.1.22 | |
| **M13** | **10.10.1.23** | **已連線** |
| **M14** | **10.10.1.24** | **已連線** |
| M15 | 10.10.1.25 | |
| M16 | 10.10.1.26 | |
| M17 | 10.10.1.27 | |
| M18 | 10.10.1.28 | |

> 注意：沒有 M19、M20。M01~M18 連續 18 台。

---

## 3. Drive.Log 格式定義

### 檔案位置
```
SMB 共享路徑：\\{ip}\LOG\{DD}Drive.Log     ← 注意 LOG 大寫
本地備份路徑：C:\DrillLogs\{machine_id}\{YYYYMMDD}\{DD}Drive.Log
```
- `{DD}` = 01~31（日期前綴，每月循環覆蓋）
- 例：`\\10.10.1.23\LOG\06Drive.Log`（4 月 6 日的 LOG）

### CSV 欄位（逗號分隔，23 欄，0-based index）

| Col | 名稱 | 範例值 | 用途 |
|-----|------|--------|------|
| 0 | 日期 | 2026/04/02 | 時間歸屬 |
| 1 | 時間 | 10:56:35 | 時間歸屬 |
| 2 | 模式 | AUTO / MAN | 參考 |
| **3** | **狀態** | **RUN / RESET / STOP** | **稼動率核心** |
| 4 | 程式名 | O100.txt / 空白 | 參考 |
| 5 | X 座標 | 630.000 | — |
| 6 | Y 座標 | 270.000 | — |
| 7 | 刀具號 | 000 / 084 | 參考 |
| **8** | **針徑(mm)** | 0.150 / 1.000 | 參考 |
| 9 | 訊息碼 | 5321 / 0000 | — |
| **10** | **累計計數器** | 173425119 | **孔數計算** |
| 11-16 | 旗標 | 1,0,0,0,0,0 | — |
| 17 | Z 軸位置 | -58.166 / 0.000 | — |
| 18-22 | 預留 | 0.000 | — |

### 範例行
```csv
2026/04/02,10:56:35,MAN,RESET,,   630.000,   270.000,000, 0.150,5321,173425119,1,0,0,0,0,0,  0.000,  0.000,  0.000,  0.000,  0.000,  0.000
2026/04/02,11:55:57,AUTO,RUN,O100.txt,    20.142,   276.228,084, 1.000,0000,173425171,1,0,0,0,0,0,-55.200,  0.000,  0.000,  0.000,  0.000,  0.000
2026/04/02,11:53:21,AUTO,STOP,O100.txt,   629.999,   269.998,084, 1.000,8424,173425122,1,0,0,0,0,0,  0.000,  0.000,  0.000,  0.000,  0.000,  0.000
```

### 關鍵特性
- 編碼：UTF-8
- 每秒一行，一整天約 77,000~86,400 行，約 10~12 MB
- **跨夜處理**：31Drive.Log 可能包含前一天 23:52 的資料
  → 必須用 col 0（日期）判斷歸屬，不用檔名
- **月初覆蓋**：每月循環，5/1 覆蓋 01*.Log
  → 用 `parse_progress.file_size` 偵測：檔案突然變小 = 被覆蓋 = 從頭重新解析
- **欄位有前導空格**：如 `   630.000`，解析時需 `.strip()`

---

## 4. 計算邏輯

### 稼動率
```
稼動率 = RUN 秒數 ÷ 該時段總記錄秒數 × 100%
```

| col 3 值 | 意義 | 計入稼動 |
|----------|------|----------|
| RUN | 鑽孔中 | ✅ 是 |
| RESET | 閒置/待機 | ❌ 否 |
| STOP | 暫停 | ❌ 否 |

### 孔數
```
某時段孔數 = 該時段最後一筆 col10 - 第一筆 col10
```
- col 10 是遞增累計計數器
- 換刀/重啟時可能重置（計數器突然變小）
- Parser 需偵測此情況，視為新計數週期，不產生負值

### 顆粒度
- **每小時**：統計 RUN/RESET/STOP 秒數 + 孔數
- **每日**：彙總 24 小時
- **每週/每月**：從每日數據彙總

---

## 5. SQLite Schema

### 資料庫檔案：`drill_monitor.db`

```sql
-- ===== 核心表 =====

-- 每小時稼動率 + 孔數
CREATE TABLE hourly_utilization (
    machine_id    TEXT NOT NULL,     -- 'M01'~'M18'
    date          TEXT NOT NULL,     -- 'YYYY-MM-DD'
    hour          INTEGER NOT NULL,  -- 0~23
    run_seconds   INTEGER DEFAULT 0,
    reset_seconds INTEGER DEFAULT 0,
    stop_seconds  INTEGER DEFAULT 0,
    total_seconds INTEGER DEFAULT 0,
    utilization   REAL DEFAULT 0.0,  -- 0.0~100.0
    hole_count    INTEGER DEFAULT 0,
    PRIMARY KEY (machine_id, date, hour)
);

-- 機台當前狀態（parser 每次覆寫）
CREATE TABLE machine_current_state (
    machine_id    TEXT PRIMARY KEY,
    state         TEXT,              -- 'RUN' / 'RESET' / 'STOP'
    mode          TEXT,              -- 'AUTO' / 'MAN'
    program       TEXT,              -- 'O100.txt' / ''
    tool_num      TEXT,              -- '084' / '000'
    drill_dia     REAL,              -- 0.150
    since         TEXT,              -- ISO timestamp：狀態開始時間
    last_update   TEXT,              -- ISO timestamp：最後更新
    counter       INTEGER            -- col 10 累計值
);

-- 狀態轉換事件（用於停機分析）
CREATE TABLE state_transitions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id    TEXT NOT NULL,
    timestamp     TEXT NOT NULL,     -- ISO timestamp
    from_state    TEXT,
    to_state      TEXT NOT NULL,
    program       TEXT,
    tool_num      TEXT,
    drill_dia     REAL
);

-- ===== 系統表 =====

-- 連線健康偵測
CREATE TABLE machine_health (
    machine_id        TEXT PRIMARY KEY,
    is_online         INTEGER DEFAULT 0,  -- 0/1
    last_seen         TEXT,               -- 最後成功通訊
    offline_since     TEXT,               -- NULL = online
    consecutive_fails INTEGER DEFAULT 0,
    last_check        TEXT
);

-- 增量解析進度（防重複解析）
CREATE TABLE parse_progress (
    machine_id    TEXT NOT NULL,
    day_prefix    TEXT NOT NULL,     -- '01'~'31'
    last_line     INTEGER DEFAULT 0,
    last_timestamp TEXT,
    file_size     INTEGER DEFAULT 0,
    PRIMARY KEY (machine_id, day_prefix)
);

-- ===== 索引 =====
CREATE INDEX idx_hourly_date ON hourly_utilization(date);
CREATE INDEX idx_transitions_ts ON state_transitions(machine_id, timestamp);
```

### 自動維護機制
1. **SQLite 自動歸檔**：parser 啟動時檢查 db 檔案大小，超過 500MB 自動將 6 個月前資料搬到 `archive_YYYY.db`
2. **本地 LOG 備份自動清理**：robocopy 備份的 LOG 檔超過 90 天自動刪除

---

## 6. HTTP API 端點

### 基本資訊
- 綁定：`{office_NIC_IP}:8080`（TODO：確認 office NIC IP）
- 所有回應：`Content-Type: application/json; charset=utf-8`

### GET /api/drilling/overview
**用途**：Dashboard「機台總覽」tab

```json
{
  "timestamp": "2026-04-02T16:31:56",
  "machines": [
    {
      "id": "M13",
      "state": "RUN",
      "mode": "AUTO",
      "program": "O100.txt",
      "tool_num": "084",
      "drill_dia": 1.0,
      "since": "2026-04-02T11:55:57",
      "duration_minutes": 276,
      "util_today": 72.5,
      "hole_count_today": 15230,
      "counter": 173430612
    }
  ],
  "summary": {
    "running": 2,
    "idle": 0,
    "stopped": 0,
    "offline": 16,
    "total": 18
  },
  "health": [
    {
      "id": "M13",
      "is_online": true,
      "last_seen": "2026-04-02T16:31:00"
    },
    {
      "id": "M01",
      "is_online": false,
      "offline_since": null,
      "last_seen": null
    }
  ]
}
```

### GET /api/drilling/utilization?period=day&date=2026-04-02
**用途**：Dashboard「稼動排行」tab

```json
{
  "period": "day",
  "date": "2026-04-02",
  "machines": [
    {
      "id": "M13",
      "utilization": 72.5,
      "run_seconds": 13629,
      "total_seconds": 18792,
      "hole_count": 15230
    }
  ],
  "fleet_average": 65.3,
  "target": 75
}
```

**period 參數**：
- `day` + `date=YYYY-MM-DD`：該日每台稼動率
- `week` + `date=YYYY-MM-DD`：該週每日平均
- `month` + `date=YYYY-MM`：該月每週平均

### GET /api/drilling/heatmap?date=2026-04-02
**用途**：Dashboard「稼動分析」tab（24 小時熱力圖）

```json
{
  "date": "2026-04-02",
  "machines": [
    {
      "id": "M13",
      "hours": [
        {"hour": 0, "utilization": 86.2, "hole_count": 1200},
        {"hour": 1, "utilization": 0.0, "hole_count": 0},
        {"hour": 8, "utilization": 63.9, "hole_count": 980}
      ]
    }
  ]
}
```

### GET /api/drilling/transitions?machine=M13&date=2026-04-02
**用途**：除錯 / 停機分析

```json
{
  "machine_id": "M13",
  "date": "2026-04-02",
  "transitions": [
    {
      "timestamp": "2026-04-02T11:53:21",
      "from": "RESET",
      "to": "STOP",
      "program": "O100.txt"
    }
  ]
}
```

---

## 7. Parser 核心邏輯（drive_log_parser.py）

### 核心流程
```
1. 載入 config（機台清單、IP、路徑）
2. 對每台已啟用的機台：
   a. 判斷今天的 day_prefix（如 "06"）
   b. 讀取 parse_progress 表取得上次解析位置
   c. 開啟本地備份的 {DD}Drive.Log
   d. 從 last_line 位置繼續讀（增量解析）
   e. 逐行解析 CSV（注意前導空格要 strip）
   f. 統計每小時 RUN/RESET/STOP 秒數
   g. 計算每小時孔數（col10 差值，偵測重置）
   h. 偵測狀態轉換事件
   i. 更新 machine_current_state（覆寫）
   j. UPSERT hourly_utilization
   k. INSERT state_transitions
   l. 更新 parse_progress
3. 更新 machine_health（ping 或檔案存在性）
4. 檢查是否需要自動歸檔
5. 等待 10 分鐘，重複
```

### 增量解析重點
- Drive.Log 是 append-only（只在尾部新增行）
- 用 `parse_progress.last_line` 跳過已處理的行
- 用 `parse_progress.file_size` 偵測覆蓋事件
  - 新月第一天 file_size 突然變小 → 重置 last_line=0，從頭解析

### 跨日判斷
```python
today = datetime.date.today()
day_prefix = today.strftime("%d")  # "06"
# LOG 裡的日期可能是昨天（跨夜情況）
# 必須用 col 0 的實際日期做歸屬
```

### 去重邏輯
robocopy 會重複拷貝正在寫入的檔案。`parse_progress` 表記錄了 `last_line`，確保不會重複計算。

---

## 8. LOG 收集（log_collector.bat / .py）

### robocopy 指令
```bat
robocopy \\10.10.1.23\LOG C:\DrillLogs\M13\%date:~0,4%%date:~5,2%%date:~8,2% *Drive.Log /R:1 /W:1
robocopy \\10.10.1.24\LOG C:\DrillLogs\M14\%date:~0,4%%date:~5,2%%date:~8,2% *Drive.Log /R:1 /W:1
```

### SMB 連線（等全部線路安裝好後一次執行）
```bat
@echo off
REM === SMB 連線設定 — 一次性執行 ===
net use \\10.10.1.11\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.12\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.13\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.14\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.15\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.16\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.17\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.18\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.19\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.20\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.21\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.22\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.23\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.24\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.25\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.26\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.27\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.28\LOG /user:Takeuchi "" /persistent:yes 2>nul
echo Done. 驗證: net use
pause
```

### 90 天備份自動清理
```python
# 清理邏輯：刪除 C:\DrillLogs\ 下超過 90 天的子目錄
import os, time, shutil
BACKUP_ROOT = r"C:\DrillLogs"
MAX_AGE_DAYS = 90
cutoff = time.time() - MAX_AGE_DAYS * 86400
for machine_dir in os.listdir(BACKUP_ROOT):
    machine_path = os.path.join(BACKUP_ROOT, machine_dir)
    if not os.path.isdir(machine_path):
        continue
    for date_dir in os.listdir(machine_path):
        date_path = os.path.join(machine_path, date_dir)
        if os.path.isdir(date_path) and os.path.getmtime(date_path) < cutoff:
            shutil.rmtree(date_path)
```

---

## 9. Dashboard 前端

### 技術
- 單檔 HTML：`dashboard.html`
- React 18 + Babel 7（CDN 載入）
- 字型：Noto Sans TC / Microsoft JhengHei
- 自動每 10 分鐘 fetch API 更新數據
- 右上角顯示下次更新倒數

### 四個 Tab

**Tab 1：機台總覽**
- 色塊 tile 網格，色彩代表狀態：RUN=綠 / IDLE=橘 / STOP=紅 / MAINT=紫 / OFFLINE=灰
- 每張 tile 整塊頂部用狀態色填滿（遠處可辨識）
- 顯示：機台編號、狀態標籤、持續時間
- 閒置超過 30 分鐘顯示「⚠ 閒置過久」
- 頂部 KPI 條：稼動中 X 台 / 閒置 X 台 / 停機 X 台

**Tab 2：稼動排行**
- 頂部大字：全機台平均稼動率
- 月 → 週 → 日 下鑽式直條圖（麵包屑導航）
- 每根柱子顯示稼動率數字 + 目標線 75% + 孔數
- 柱子顏色：≥75% 綠、50-74% 橘、<50% 紅

**Tab 3：稼動分析**
- 上半部：機台稼動率排行表（橫條圖，附孔數）
- 下半部：24 小時熱力圖
  - X 軸 = 0~23 小時，Y 軸 = 每台機台
  - 色階：深綠 ≥75%、淺綠 50-74%、黃 25-49%、紅 <25%、灰 = 0%
  - 日班（08-20）/ 夜班（20-08）分界線
  - Filter：可選只顯示 <25% 的格子（高亮問題時段）
  - Hover 顯示「機台｜小時｜稼動率%｜日班/夜班」

**Tab 4：作業細節**（Phase F，初版不實作）
- 顯示佔位訊息：「此功能將在 Phase F 實作」

### 前端與 API 的對應
| Tab | API 端點 |
|-----|---------|
| 機台總覽 | GET /api/drilling/overview |
| 稼動排行 | GET /api/drilling/utilization?period=... |
| 稼動分析 | GET /api/drilling/heatmap?date=... |

### Mock 數據
初版前端在 API 未就緒時使用內建 mock 數據顯示。當 `fetch()` 失敗時 fallback 到 mock。

---

## 10. 目錄結構

```
drill-monitoring/
├── README.md
├── config/
│   ├── machines.json          # 機台清單 + IP + 啟用狀態
│   └── settings.json          # 抓取間隔、路徑、閾值等
├── parsers/
│   ├── base_parser.py         # 共用邏輯（跳行、去重、歸檔）
│   └── drive_log_parser.py    # Takeuchi Drive.Log 解析
├── collector/
│   ├── log_collector.py       # robocopy 排程（或 .bat）
│   ├── smb_setup.bat          # 一次性 net use 設定
│   └── health_check.py        # 連線偵測
├── server/
│   └── api_server.py          # HTTP API（stdlib http.server）
├── web/
│   └── dashboard.html         # 單檔前端
├── db/
│   └── schema.sql             # SQLite schema（上方第 5 節）
├── tools/
│   ├── log_probe.py           # LOG 探測分析工具（已完成）
│   ├── cleanup.py             # 90 天備份清理
│   └── archive.py             # SQLite 自動歸檔
└── tests/
    ├── test_parser_accuracy.py # Golden test（3/17 驗證數據）
    └── fixtures/
        └── 17Drive.Log        # M02 的 3/17 真實 LOG（需手動放入）
```

### config/machines.json 範例
```json
{
  "machines": [
    {"id": "M01", "ip": "10.10.1.11", "enabled": false},
    {"id": "M02", "ip": "10.10.1.12", "enabled": false},
    {"id": "M13", "ip": "10.10.1.23", "enabled": true},
    {"id": "M14", "ip": "10.10.1.24", "enabled": true},
    {"id": "M18", "ip": "10.10.1.28", "enabled": false}
  ],
  "log_share_name": "LOG",
  "smb_user": "Takeuchi",
  "smb_password": ""
}
```

### config/settings.json 範例
```json
{
  "poll_interval_seconds": 600,
  "backup_root": "C:\\DrillLogs",
  "db_path": "drill_monitor.db",
  "db_archive_threshold_mb": 500,
  "backup_retention_days": 90,
  "http_host": "TODO_OFFICE_NIC_IP",
  "http_port": 8080,
  "utilization_target": 75
}
```

---

## 11. 擴展性設計要點

### 未來需支援 Kataoka 雷射鑽孔機（4 台）
- LOG 格式不同（待操作員填寫問卷後確認）
- **Parser 層做分離**：
  - `parsers/drive_log_parser.py` — Takeuchi
  - `parsers/laser_log_parser.py` — Kataoka（未來新增）
- **兩種 parser 寫入同一套 SQLite schema**
  - `hourly_utilization`、`machine_current_state` 等核心表通用
  - Takeuchi 特有欄位（針徑 col8 等）放在 `machine_current_state` 裡，Kataoka 可以填 NULL
- **machines.json 加 `type` 欄位**：
  ```json
  {"id": "L01", "ip": "10.10.1.29", "type": "kataoka", "enabled": false}
  ```
- Dashboard 和 API 層完全不用改——它們只讀 SQLite

---

## 12. Golden Test 驗證數據

以下是 M02（DRILL-01）2026/03/17 的驗證數據，來自列印報告（維修人員確認）：

### 每小時 RUN/RESET/STOP 秒數
```
Hour  RUN    RESET   STOP   Total   Util%
00    3103   436     61     3600    86.2
01    0      0       0      0       0.0
02    0      0       0      0       0.0
03    0      0       0      0       0.0
04    0      0       0      0       0.0
05    0      0       0      0       0.0
06    0      0       0      0       0.0
07    274    3319    7      3600    7.6
08    2302   1284    14     3600    63.9
09    2733   860     7      3600    75.9
10    3319   257     24     3600    92.2
11    1898   1594    108    3600    52.7
12    0      3600    0      3600    0.0
13    0      3600    0      3600    0.0
14    0      3600    0      3600    0.0
15    0      3600    0      3600    0.0
16    0      2797    0      2797    0.0
17    0      1303    0      1303    0.0
18    0      0       0      0       0.0
```

### 整日合計
```
RUN=13629  RESET=26250  STOP=221  Total=40100（非 86400，因為部分時段無記錄）
整日稼動率 = 13629 / 67405 = 20.2%（含無記錄時段的分母）
```

### 狀態轉換次數
```
共 63 次轉換
```

---

## 13. TODO 清單（部署前）

- [ ] 確認運算主機 OS 為 Win10
- [ ] 確認運算主機 office network NIC IP → 更新 settings.json 的 http_host
- [ ] 全部 18 台線路安裝完成後執行 smb_setup.bat
- [ ] 在 M13、M14 上驗證 `\\10.10.1.23\LOG\` 可存取
- [ ] 將 M02 的 17Drive.Log 放入 tests/fixtures/ 執行 golden test
- [ ] IP forwarding 確認已關閉
- [ ] HTTP server 確認未綁 0.0.0.0

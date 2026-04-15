# 鑽孔機稼動監控系統 - 部署指南

## 前置條件

- **Windows 10** (64-bit)
- **Python 3.8+** (從 python.org 標準安裝，安裝時勾選 "Add to PATH")
- **不需要 pip install**，全部使用 Python 標準庫
- 網路可連到鑽孔機 SMB 分享 (10.10.1.x / 10.10.2.x)

### 驗證 Python 安裝

```cmd
python --version
```

預期輸出：`Python 3.8.x` 或更新。如果 `python` 找不到，試 `python3 --version` 或確認 Python 已加入 PATH。

---

## 快速啟動

1. 把整個 `deploy/` 資料夾複製到測試電腦，例如 `C:\DrillMonitor\`

2. 開 Command Prompt，進入資料夾：
   ```cmd
   cd C:\DrillMonitor
   ```

3. 初始化資料庫：
   ```cmd
   python db\init_db.py
   ```
   應該會看到建立的 table 清單。

4. 跑 parser 測試確認 Python 環境正常：
   ```cmd
   python -m unittest tests.test_parser_accuracy -v
   ```
   應該 5 個 test 全部 PASS。

5. 檢查機台網路連線：
   ```cmd
   python collector\health_check.py
   ```
   會顯示各機台 online/offline 狀態。

6. 啟動 Dashboard（僅 API server，不收 LOG）：
   ```cmd
   python main.py --server-only
   ```
   瀏覽器開 http://127.0.0.1:8080 看 Dashboard。

7. 跑一次完整的收集+解析：
   ```cmd
   python main.py --once
   ```

8. 啟動完整系統（持續收集 + 解析 + Dashboard）：
   ```cmd
   python main.py
   ```

---

## 設定檔說明

### config/settings.json

| 欄位                     | 預設值             | 說明                                        | 需要改？ |
|--------------------------|--------------------|--------------------------------------------|---------|
| `poll_interval_seconds`  | `600`              | 每次收集間隔（秒），600 = 10 分鐘            | 通常不用 |
| `backup_root`            | `C:\DrillLogs`     | LOG 備份目錄                                | 確認路徑存在 |
| `db_path`                | `drill_monitor.db` | SQLite 資料庫檔案（相對於專案根目錄）         | 通常不用 |
| `http_host`              | `127.0.0.1`        | Dashboard 綁定 IP                           | **要改** |
| `http_port`              | `8080`             | Dashboard 埠號                              | 通常不用 |
| `backup_retention_days`  | `90`               | 備份保留天數                                 | 通常不用 |
| `utilization_target`     | `75`               | Dashboard 稼動率目標線 (%)                   | 視需求調整 |
| `log_file`               | `drill_monitor.log`| 應用程式 LOG 檔案                           | 通常不用 |

#### ⚠ 重要：上線前必須修改 `http_host`

測試時用 `127.0.0.1` 即可（只有本機能看 Dashboard）。

**正式上線時**，改成這台電腦在辦公室網路的 IP，讓其他電腦也能看 Dashboard：

```json
"http_host": "10.10.1.100"
```

查 IP 方法：在 Command Prompt 跑 `ipconfig`，找辦公室網卡的 IPv4 位址。

注意：**不要填 `0.0.0.0`**，程式會拒絕並自動 fallback 到 127.0.0.1。

### config/machines.json

目前啟用的機台：

| 機台 ID | IP           | 類型     | 狀態    |
|---------|-------------|----------|---------|
| M13     | 10.10.1.23  | Takeuchi | enabled |
| M14     | 10.10.1.24  | Takeuchi | enabled |
| L2      | 10.10.2.12  | Kataoka  | enabled |
| L3      | 10.10.2.13  | Kataoka  | enabled |

- 要啟用其他機台：把該機台的 `"enabled": false` 改成 `"enabled": true`
- 要停用機台：改成 `"enabled": false`

SMB 連線帳號（檔案底部）：
- Takeuchi 機台：user = `Takeuchi`，密碼空白
- Kataoka 機台：user = `KATAOKA`，密碼空白

---

## 執行模式

| 指令                          | 用途                                           |
|-------------------------------|-----------------------------------------------|
| `python main.py`              | 完整系統：持續收集 + 解析 + Dashboard           |
| `python main.py --once`       | 單次收集 + 解析，完成後結束（適合 Task Scheduler）|
| `python main.py --server-only`| 僅啟動 Dashboard（不收集、不解析）              |

### Windows 工作排程器 (Task Scheduler)

如果不想讓程式一直跑，可以用 Task Scheduler 定期執行：

- **動作**: `python`
- **引數**: `C:\DrillMonitor\main.py --once`
- **起始位置**: `C:\DrillMonitor`
- **排程**: 每 10 分鐘（上班時間）

---

## 系統功能

- **增量解析**：只處理新的 LOG 行，不重複解析
- **稼動率計算**：每小時 RUN/RESET/STOP 秒數 → 稼動率 %
- **孔數計算**：Takeuchi = Drive.Log counter delta / Kataoka = LSR Count
- **健康檢查**：SMB 連線狀態追蹤
- **工單追蹤**：Takeuchi O-prefix program / Kataoka ProcTimeEnd
- **跨午夜處理**：state transition 和 laser RUN carryover
- **自動清理**：90 天前備份自動刪除
- **DB 歸檔**：DB 超過 500MB 時自動歸檔 6 個月前資料
- **Dashboard**：4 個頁面 — 機台總覽、稼動排行、稼動分析、作業細節

---

## 疑難排解

### Dashboard 無法啟動
確認 `config/settings.json` 的 `http_host` 是有效 IP（不是 `TODO_OFFICE_NIC_IP`）。

### 無法連線到機台
1. 跑 `python collector\health_check.py` 檢查連線
2. 確認 `config/machines.json` 裡的 IP 正確
3. 手動測試 SMB：`dir \\10.10.1.23\LOG`

### 資料庫錯誤
刪除 `drill_monitor.db`，重跑 `python db\init_db.py` 重建。

### robocopy 找不到
Windows 10 內建 robocopy，確認是在 Command Prompt 執行（不是 PowerShell ISE）。

---

## 檔案結構

```
deploy/
├── main.py                 # 主程式入口
├── DEPLOY_README.md        # 本文件
├── config/
│   ├── settings.json       # 系統設定（間隔、路徑、IP）
│   └── machines.json       # 機台清單（IP、類型、啟用狀態）
├── parsers/
│   ├── base_parser.py      # 共用工具（設定載入、DB 連線、增量追蹤）
│   ├── drive_log_parser.py # Takeuchi 機鑽 Drive.Log 解析
│   ├── tx1_log_parser.py   # Takeuchi TX1.Log 工單追蹤
│   └── laser_log_parser.py # Kataoka 雷鑽 LOG 解析
├── collector/
│   ├── log_collector.py    # Takeuchi 機台 LOG 收集（robocopy）
│   ├── laser_log_collector.py # Kataoka 雷鑽 LOG 收集
│   └── health_check.py     # 機台連線檢查
├── server/
│   └── api_server.py       # HTTP API + Dashboard 伺服器
├── db/
│   ├── init_db.py          # 資料庫初始化 + 遷移
│   └── schema.sql          # SQLite schema（6 張 table）
├── web/
│   └── dashboard.html      # Dashboard 前端（React 18）
├── tools/
│   ├── cleanup.py          # 備份清理工具
│   └── archive.py          # 資料庫歸檔工具
└── tests/
    └── test_parser_accuracy.py  # Parser 正確性測試
```

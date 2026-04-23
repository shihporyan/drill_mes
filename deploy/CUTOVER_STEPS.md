# Cutover 手順 — 2026-04-22 正式抓 LOG

目標：把最新的 `deploy/` 放到運算電腦上，清掉舊 DB，從今天下午 16:00 開始長期跑，
並確保跳電後自動重啟後能繼續收 LOG。

---

## 已確認的環境

- 運算電腦 IP：`10.10.1.2`（同子網 10.10.1.x，跟鑽孔機同一段）
- 自動登入：**已設定**，跳電後不用手動輸入密碼
- 現有自動啟動機制：`HKCU\...\Run\DrillMonitor` → `C:\DrillMonitor\start_monitor.bat`
  （保留此 Registry entry，本次只會**覆蓋 bat 內容**，不動 Registry）

---

## Step 1 — 停掉運行中的 DrillMonitor

運算電腦上：

```cmd
taskkill /F /FI "WINDOWTITLE eq DrillMonitor"
```

（若舊 bat 沒設 title，上面殺不掉。那就用工作管理員手動關掉 `python.exe`，或下：）

```cmd
wmic process where "CommandLine like '%main.py%'" call terminate
```

## Step 2 — 備份舊 DB（可選）

若想保留 4/22 之前的歷史：

```cmd
copy C:\DrillMonitor\drill_monitor.db C:\DrillMonitor\drill_monitor.db.bak_20260422
```

## Step 3 — 覆蓋 deploy/ 到 C:\DrillMonitor\

把 Mac 上的 `/Users/ryanhsu/Documents/drill_mes/deploy/` 所有內容複製到
`C:\DrillMonitor\`（覆蓋同名檔）。新版會帶來：

- 新的 `parsers/mtime_observer.py`（Layer 1 flush 觀察）
- 新的 DB schema（多了 `tx1_mtime_events`、`tx1_event_latency`、`log_file_observe` 三張表）
- 新的 `start_monitor.bat`（title=DrillMonitor + M01-M18 net use + 180s grace）
- 新的 `config/machines.json`（M01-M18 全 enabled）
- 新的 `config/settings.json`（`http_host: "10.10.1.2"`）
- 隱藏「作業細節」頁面
- `SETUP_NET_USE.bat`、`CUTOVER_STEPS.md`、`FRESH_DEPLOY.bat`

## Step 4 — 建立 M01-M18 的持久 SMB 連線

```cmd
cd C:\DrillMonitor
SETUP_NET_USE.bat
```

會清掉舊的 10.10.1.23 / .24 映射，幫 M01-M18 全部重新建立 `/persistent:yes` 映射。

驗證：

```cmd
dir \\10.10.1.11\LOG
dir \\10.10.1.23\LOG
dir \\10.10.1.28\LOG
```

抽三台不同範圍看能否列出檔案。

## Step 5 — 清 DB、重建

```cmd
cd C:\DrillMonitor
FRESH_DEPLOY.bat
```

此腳本會：
- 刪 `drill_monitor.db` / `-wal` / `-shm` / `drill_monitor.log*`
- 重建 DB schema（`python db\init_db.py`）
- 跑 parser 單元測試
- 跑 `collector\health_check.py` 確認 M01-M18 連線 OK

**這步會把所有歷史資料清空。** 從現在起 DB 只有 2026-04-22 16:00 之後的事件。

## Step 6 — 手動第一次啟動

```cmd
cd C:\DrillMonitor
start_monitor.bat
```

注意：`start_monitor.bat` 會先等 180 秒再動（原本舊 bat 就是這樣，保留當作
開機後的網路暖機時間）。若你想馬上看效果，手動啟動時可以直接跑：

```cmd
cd C:\DrillMonitor
python main.py
```

會開一個標題 `DrillMonitor` 的視窗。每 10 分鐘看到一輪：

```
--- Collection cycle ---
[M01] robocopy OK (exit code 0)
...
```

Dashboard 打開 `http://10.10.1.2:8080`。

## Step 7 — 重開機驗證

趁現場還方便時測一次跳電復原：

1. 關掉 `DrillMonitor` 視窗
2. 重開機
3. 等自動登入進桌面
4. **等 180 秒**後，跳出 `DrillMonitor` 視窗並開始收 LOG
5. Dashboard 確認有新資料

---

## 疑難排解

| 問題 | 處置 |
|---|---|
| `robocopy` exit code 8+ | SMB 連不上。重跑 `SETUP_NET_USE.bat`，或手動 `dir \\IP\LOG` 驗證。 |
| 開機後沒跳出 DrillMonitor 視窗 | 檢查 `reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run"` 還有沒有 `DrillMonitor` 項目。 |
| Dashboard 連不上 | 確認其他電腦在 10.10.1.x 子網；`settings.json` 的 `http_host` 是 `10.10.1.2`。 |
| 想回到乾淨狀態 | 再跑 `FRESH_DEPLOY.bat`（只清 DB，不動 LOG 備份）。 |
| 只想重啟不清 DB | 關視窗 → 跑 `start_monitor.bat`（或 `python main.py`）。 |

---

## 現在運行的組件（這版本）

1. **Collector + Parser loop**（10 min 週期）— 主工作執行緒
2. **TX1 mtime observer**（30s 週期）— Layer 1 觀察，寫 `tx1_mtime_events`，
   驗證 state→flush 相關性。輕量，只 `os.stat`
3. **HTTP API server + Dashboard**（主執行緒）
   - 已隱藏「作業細節」tab

Layer 2（state-transition-triggered TX1 parse）尚未啟用，等這批資料累積 3-5 天
再驗 Layer 1 假設，通過後才實作。見
[notes/tx1_transition_triggered_parse_plan.md](../notes/tx1_transition_triggered_parse_plan.md)。

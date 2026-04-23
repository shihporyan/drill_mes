# Layer 2 優化計畫：State-Transition-Triggered TX1 Parse

**狀態（2026-04-23 更新）**：**擱置，原動機已失效。**

**依賴**：[tx1_flush_latency_investigation.md](tx1_flush_latency_investigation.md)（見其開頭的「2026-04-23 重大修正」）

---

## ⚠️ 2026-04-23 修正：原動機站不住

本計畫原本要解決的「工單切換最壞 40 min 延遲」，重新分析後發現：

1. **「buffer 累積 30+ min」** 是 SMB mtime lazy update 的假象，實際寫入 size 有持續推進
2. **真實 end-to-end 延遲 = parser cycle (10min) + 前端 refresh (10min) = 最大 20 min**，不含 flush 延遲
3. **便宜的優化路徑（若真的需要更即時）**：
   - **A（零代價）**：前端 `REFRESH_INTERVAL` 600 → 30/60 秒，最大延遲降到 0–10 min
   - **B（中代價）**：parser `poll_interval_seconds` 600 → 120/180 秒，最大延遲降到 2–5 min（代價：SMB 查詢流量 × 3~5）
   - **C（本文件方案）**：transition-triggered watcher thread，複雜度高，如果 A+B 已夠就不需要

**建議：** 4/20 go-live 後先觀察現場是否嫌慢。嫌慢優先做 A，不夠再做 B。本計畫 C 擱置。

以下原文保留作設計參考，**不要直接實作**，需要時重新評估動機。

---

## 要解決的問題

目前工單切換偵測延遲最壞可達 **~40 分鐘**，三個組件疊加：

```
事件發生 → [buffer 0~30+ min] → [SMB 傳播 ~3 min] → [parser cycle 0~10 min]
```

Layer 1 confirmed（4/20 測試）：**state 轉態幾乎必定觸發 TX1 flush**（RUN→STOP 後平均 13 秒 mtime 跳動）。這代表**事件發生到 SMB 傳播這段其實很快**（~3 min），真正慢的是「等下一個 10 min parser cycle」。

**Layer 2 的目標**：把 parser cycle 這段壓縮，讓工單切換後 **<5 分鐘** 就能被偵測、寫入 DB。

---

## 推薦方案：新增獨立快輪 thread

跟主 cycle 分開，專門 watch Drive.Log 的狀態轉態。

### 設計

在 `main.py` 啟動一個新的 daemon thread `state_transition_watcher`：

```
while True:
    對每台 enabled Takeuchi：
        stat 遠端 {DD}Drive.Log（SMB，僅看 size/mtime）
        if size 或 mtime 比上次有變：
            copy Drive.Log 尾段 (robocopy 或用 seek+read)
            parse 新增的 CSV 行、偵測 state 轉態
            if 有 RUN/STOP/RESET 變化：
                觸發「單機版」TX1 parse：
                    robocopy {DD}TX1.Log
                    parse_tx1_file 這一支
                    更新 machine_current_state
    sleep 60s
```

### 為什麼不直接縮短 poll_interval？

- **SMB 負載**：現行 10 min cycle 每次收 18 台 × 6 種 log + 解析 + cleanup，整段流程 ~10 秒。縮短到 60 秒 = 10 倍負載，沒必要。
- **主 cycle 處理很多東西**（laser collector、cleanup、health check、hourly 聚合）——多數每分鐘跑沒意義。
- **快輪只做一件事**：看 Drive.Log 有沒有轉態、轉態後抓 TX1。輕量、targeted。

### 為什麼不在現有 mtime_observer 裡做？

`mtime_observer` 只 stat 不 parse、純觀察用（Layer 1 驗證）。讓它保持只讀，避免「觀察工具同時也改 DB」語意混淆。快輪是獨立職責。

---

## 詳細實作步驟

### Step 1：新增單機版 TX1 parse 函式

在 [parsers/tx1_log_parser.py](parsers/tx1_log_parser.py) 加：

```python
def parse_single_machine_targeted(db_path, machine, settings):
    """Targeted robocopy + TX1 parse for a single machine.

    Called by state_transition_watcher when a Drive.Log transition is
    detected. Pulls fresh TX1.Log via robocopy, parses today + yesterday,
    updates machine_current_state.work_order if new event found.

    Does NOT run backfill (that's the normal cycle's job).
    """
    machine_id = machine["id"]
    ip = machine.get("ip")
    # Targeted robocopy: only this machine's TX1.Log (today + yesterday)
    # ... reuse helper from log_collector but limited to TX1
    # Then parse_tx1_file for today (ref_date=today) and yesterday
```

**關鍵**：只 robocopy TX1.Log（不要重收 Drive.Log 避免競態），只解析，不跑 backfill。

### Step 2：新增 watcher module

新檔 `parsers/state_transition_watcher.py`（類似 mtime_observer 架構）：

```python
def run_watcher_loop(db_path, interval=60, stop_event=None):
    last_drive_mtime = {}   # machine_id -> str
    last_parsed_state = {}  # machine_id -> str (last state we saw)
    
    while not stop_event.is_set():
        for machine in enabled_takeuchi:
            # Quick stat Drive.Log remote
            new_mtime = stat_remote_drive_log(machine)
            if new_mtime == last_drive_mtime.get(machine_id):
                continue
            
            # Pull the last N KB of Drive.Log, parse for state
            # Compare with last_parsed_state
            latest_state = read_drive_tail_state(machine)
            if latest_state != last_parsed_state.get(machine_id):
                logger.info("[%s] State change detected: %s -> %s, triggering TX1 parse",
                            machine_id, last_parsed_state.get(machine_id), latest_state)
                try:
                    parse_single_machine_targeted(db_path, machine, settings)
                except Exception as e:
                    logger.error("[%s] targeted TX1 parse failed: %s", machine_id, e)
                last_parsed_state[machine_id] = latest_state
            
            last_drive_mtime[machine_id] = new_mtime
        time.sleep(interval)
```

**精簡化**：不一定要完整 parse Drive.Log；只要確認「state 變了」即可。可以從 Drive.Log 尾端讀最後一行，取 CSV 第 4 欄（state）跟記憶中的比對。

### Step 3：在 main.py 啟動

類似 mtime_observer 的 pattern：

```python
from parsers.state_transition_watcher import start_watcher_thread

# run_all() 裡：
try:
    start_watcher_thread(db_path=db_path, settings=settings, machines_config=machines_config)
    logger.info("State transition watcher started (60s interval)")
except Exception as e:
    logger.warning("Failed to start state transition watcher: %s", e)
```

### Step 4：加 settings.json 開關

```json
{
  "tx1_trigger_on_transition": true,
  "transition_watcher_interval_seconds": 60
}
```

預設 `true`。有問題可隨時關掉。

---

## 併發與競態考量

### 跟主 cycle 的併發

- 主 cycle 的 drive_log_parser 和 watcher 都可能寫 state_transitions/machine_current_state
- SQLite WAL 模式支援多連線併發寫，只會有「序列化」效應（其中一個等待）
- Watcher 的 INSERT 用 INSERT OR IGNORE（state_transitions 有 UNIQUE constraint）或讓主 cycle 當 source of truth
- **更乾淨的做法**：watcher 只負責「觸發 TX1 parse」，**不寫 state_transitions**；state_transitions 仍由主 cycle 的 drive_log_parser 統一寫入

### 重複觸發

- 一分鐘內若同一台機台有多次轉態（例如 RESET→STOP→RUN 連續），watcher 只會觸發 1 次 TX1 parse（因為 stat 發現 mtime 變了、最終 state 不同就算）
- 主 cycle 的 TX1 parse 也會再跑，但 INSERT OR IGNORE 保護 tx1_event_latency，work_order UPDATE 是 idempotent

---

## 測試計畫

### 單元測試

- `test_state_transition_watcher.py`：
  - 模擬 stat 回傳固定 mtime → 不觸發
  - 模擬 stat 回傳新 mtime + 新 state → 觸發一次
  - 模擬 targeted_parse 拋例外 → watcher 不死

### Mac dev 驗證

- 因為 SMB 在 dev 不可用，`parse_single_machine_targeted` 會在 robocopy 階段 fail
- 測試可用 mock：替換 `stat_remote_drive_log` 和 robocopy 為回傳固定值

### 生產驗證（部署後 1-2 週）

用現有 `tx1_event_latency` 表做 before/after 比較：

```sql
-- 優化前（Layer 2 部署前的資料）
SELECT 
    CASE WHEN delay_seconds < 300 THEN '<5min'
         WHEN delay_seconds < 900 THEN '5-15min'
         WHEN delay_seconds < 1800 THEN '15-30min'
         ELSE '>30min' END as bucket,
    COUNT(*)
FROM tx1_event_latency
WHERE wo_matched=1
  AND detected_at BETWEEN '2026-04-22' AND '2026-05-06'  -- 優化前 2 週
GROUP BY bucket;

-- 優化後
-- 同樣查詢但用 '2026-05-06' AND '2026-05-20'
```

**成功標準**：優化後 p95 延遲 < 5 分鐘（或至少比優化前減半）。

---

## 風險與降級方案

| 風險 | 緩解 |
|---|---|
| Watcher crash 導致觸發失效 | thread 內 try/except 包住每一次 iteration；主 cycle 仍照常跑（雙保險）|
| Targeted robocopy 被 antivirus 或 SMB timeout 拖慢 | 設 30 秒 timeout；失敗就 log warning、下一輪再試 |
| 頻繁觸發造成 SMB 負擔 | interval 可調；重複觸發由 last_parsed_state cache 擋掉 |
| Watcher 誤判轉態（stat 抖動）| 拿 Drive.Log 尾端實際行 parse state 字串，不是單看 mtime |
| Deploy 後出問題 | `tx1_trigger_on_transition: false` 即關閉；主 cycle 仍能 work |

---

## 實作順序

1. **Layer 1 驗證通過**（等 3-5 天資料、Layer 1 SQL 跑出 p95 < 2min）← 目前在這
2. 寫 `parse_single_machine_targeted()` + unit test
3. 寫 `state_transition_watcher` module + unit test
4. main.py 整合 + settings 開關
5. Mac dev 驗證（mock SMB）
6. Deploy production + 1-2 週資料
7. 分析 before/after 延遲分佈、寫結論

預期工時：**Step 2-6 約 1 個工作天**。

---

## 完成後的系統 timing 模型

```
事件發生
    │ 
    ├─[幾秒]──→ 機台 OpeLog buffer（有觸發事件就 flush）
    │                   │
    │                   └─[13 秒]──→ TX1.Log 寫入、SMB mtime 更新
    │                                       │
    │                                       └─[~3 min]──→ 運算電腦 SMB stat 看到
    │                                                            │
    └─[1 秒]──→ Drive.Log 寫入 state 變化                       │
                    │                                           │
                    └─[~60 秒]──→ watcher stat 看到 mtime 跳動  │
                            │                                   │
                            └── 觸發 targeted robocopy + TX1 parse
                                    │
                                    └─[~5 秒]──→ DB work_order 更新

總延遲：~5 分鐘（SMB 傳播 + watcher 週期 + parse 時間）
```

vs. 優化前 ~10-40 分鐘（取決於 cycle 對齊 + buffer 狀態）。

---

## Action Items

- [ ] **等 Layer 1 資料**（~5 天後，約 2026-04-27 起）：跑 SQL 驗證 state 轉態→flush 延遲 p95 < 120 秒
- [ ] 若通過驗證：依上面 Step 1-7 實作
- [ ] 若不通過：回頭檢查是否有特定機台或特定轉態類型不 trigger flush，修正假設

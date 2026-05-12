# 鑽孔資料 → MES 端使用說明

**對象**：MES 系統開發者 / 資料工程師
**日期**：2026-05-12
**目的**：MES 端收到 `drill_snapshot_*.db.gz` 後，怎麼讀、怎麼跟 MES 的工單 list 對齊、有哪些已知資料品質問題要在 JOIN 時處理。

---

## 1. 整合策略總覽

```
MES 工單系統                       鑽孔 SQLite snapshot
─────────────                     ────────────────────
WO list (主鍵)         ← JOIN →   state_transitions
WO 預計上機時間                    o100_snapshots
WO 站別/路線                       machine_current_state
WO 預計點數                        hourly_utilization (機鑽)
                                  laser_work_orders   (雷鑽)
```

**核心策略**：MES 端用既有 WO list 當「**羅盤**」反查鑽孔資料。**鑽孔端不主動產 WO 表**（除了雷鑽已內建），時間切割、WO 歸屬、計算稼動率/等待時間/加工時間都由 MES 端完成。

→ 鑽孔端只負責「乾淨的原始資料」+ 「資料品質警示」。MES 端負責「業務邏輯」。

---

## 2. 收到什麼、怎麼開

### 2.1 檔案

```
D:\drill_export\drill_snapshot_YYYY-MM-DD_HHMM.db.gz
```

每次推送一顆完整的 SQLite snapshot（已用 `VACUUM INTO` atomic 切下來、不會半寫狀態）。gzip 後通常 30-50MB（原檔約 200-300MB）。

### 2.2 解開讀取

```python
import gzip, shutil, sqlite3

# 解 gzip
with gzip.open('drill_snapshot_2026-05-12_1430.db.gz', 'rb') as fin, \
     open('drill_snapshot.db', 'wb') as fout:
    shutil.copyfileobj(fin, fout)

# 直接讀（read-only 強烈建議）
conn = sqlite3.connect('file:drill_snapshot.db?mode=ro', uri=True)
```

### 2.3 重要前提

- **這是 snapshot，不是 live DB**。資料截止於 push 那一刻
- **read-only 開啟**：避免不小心改到 snapshot 影響後續比對
- **每次都是完整快照**，不是增量：直接覆蓋舊的即可（drill 端會自動保留最近 3 個）

---

## 3. 重要資料表（按 MES 用得到的優先序）

### 3.1 `laser_work_orders` ⭐ 雷鑽 — 直接可用

雷鑽的 WO 資料**已經幫你算好**，直接 JOIN 即可：

| 欄位 | 型別 | 說明 |
|---|---|---|
| `machine_id` | TEXT | `L1`-`L4` |
| `work_order` | TEXT | 工單號 |
| `start_time` | TEXT | ISO 8601 (server time) |
| `end_time` | TEXT | ISO 8601 |
| `duration_secs` | REAL | end - start (秒) |
| `hole_count` | INTEGER | 累計孔數 |
| `station` | TEXT | 站別 |
| `lsr_file_path` | TEXT | 來源 .lsr 檔路徑 |

→ MES 對 `work_order` 直接 JOIN，可信度 ~95%。

### 3.2 `state_transitions` — 機鑽 RUN/STOP 事件

機台狀態變化事件流。每一筆記錄一個 transition：

| 欄位 | 型別 | 說明 |
|---|---|---|
| `machine_id` | TEXT | `M01`-`M18` |
| `timestamp` | TEXT | ISO 8601 (**已 timezone-normalize**，見 §4.2) |
| `from_state` | TEXT | 前狀態 |
| `to_state` | TEXT | 後狀態：`RUN` / `STOP` / `RESET` |
| `program` | TEXT | 通常是 `O100.txt`（不是 WO 號） |

**計算 RUN 累計秒數**（某時間區間內）：

```sql
-- 在 [t_start, t_end] 期間 M16 的 RUN 秒數
WITH events AS (
  SELECT timestamp, to_state,
    LEAD(timestamp) OVER (ORDER BY timestamp) AS next_ts
  FROM state_transitions
  WHERE machine_id = 'M16'
    AND timestamp BETWEEN ? AND ?
)
SELECT SUM(strftime('%s', next_ts) - strftime('%s', timestamp))
FROM events WHERE to_state = 'RUN' AND next_ts IS NOT NULL;
```

### 3.3 `o100_snapshots` — 工單路由（板別/WO）

每次 O100.txt 改變時的內容快照。`active_subs` 是 sub-program 編號 JSON array，由 100 位數編碼工單：

| 欄位 | 型別 | 說明 |
|---|---|---|
| `machine_id` | TEXT | `M01`-`M18` |
| `captured_at` | TEXT | ISO 8601 |
| `trigger_source` | TEXT | `tx1_loadprogram` / `mtime_polling` / `initial` |
| `active_subs` | TEXT | JSON `[127, 128, 102]` — sub-program 編號 |
| `raw_content` | TEXT | 原始 O100.txt 內容 |
| `tx1_event_ts` | TEXT | 對應的 TX1 事件時間（若有） |

**怎麼從 `active_subs` 推 WO**：
- 每個 M98P### 對應一張板；100 位數通常 = WO 的尾數
- 例：`[127, 128, 102]` → 三個 sub-program 都在 100-199 範圍 → 同一張 WO 的 3 個 sub
- **跨多個百位範圍時 = 併版**（多張 WO 合進同一 O100）— 見 §4.5

### 3.4 `machine_current_state` — 機鑽當前 snapshot

每台機**只有一行**，當前狀態。**沒有歷史**：

| 欄位 | 說明 |
|---|---|
| `machine_id` | M01-M18 |
| `state` | RUN/STOP/RESET |
| `work_order` | 當下 WO 號（O-prefixed，e.g. `O2604056`） |
| `since` | 進入當前 state 的時間 |
| `last_update` | DB 最後寫入時間 |
| `current_o100_subs` | 當前 active_subs JSON |
| `counter` | 當前孔數計數器（**會 reset 不要做時間差**） |

→ 用途：確認推送當下的機台狀態。不要拿來做歷史分析。

### 3.5 `hourly_utilization` — 機鑽小時級彙總

| 欄位 | 說明 |
|---|---|
| `machine_id`, `date`, `hour` | 主鍵 |
| `run_seconds` | 該小時 RUN 累計秒數 |
| `reset_seconds`, `stop_seconds` | 同上 |
| `utilization` | `run / (run+reset+stop)` |
| `hole_count` | **該小時鑽的孔數**（已扣除 reset 重算雜訊） |

→ **這是機鑽目前唯一有 hole_count 的歷史表**。**但是按小時 bucket，不是按 WO bucket**（重要 caveat，見 §4.6）。

---

## 4. 資料品質警示（**必讀，會影響 JOIN 對得起來率**）

按嚴重度排：

### 4.1 ⚠️ O100 孤兒編輯 ~60% (機鑽 only)

**現象**：操作員在控制電腦上修 O100.txt 時會多次儲存草稿，鑽孔端 TX1 LoadProgram 事件**只 hook 到最後一次 LOAD**，前面草稿全部沒紀錄。實測約 **60% 的編輯**在 `o100_snapshots` 沒有對應事件。

**對 MES 影響**：
- `o100_snapshots` 可能**漏掉**某些 WO 切換點
- 「機台從 14:00-16:00 在跑 WO_X」這個推論可能是錯的（中間其實切換過）
- 但 mtime polling backstop 會在 5 分鐘內補抓，所以**不會永久漏**，只是時間點不準

**MES 端對策**：
- 不要假設「兩個 o100_snapshots 之間時間都是同一張 WO」
- 用 MES 的 WO 上機時間當主，鑽孔資料當「驗證」而非「source of truth」
- 信心度標記：兩個 snapshot 間隔 > 30 min 且機台一直 RUN → 中等信心；間隔 < 5 min → 高信心

### 4.2 ⚠️ TX1 Timezone Per-Machine 不一致

**現象**：18 台 Takeuchi 機台的 TX1 內部時鐘 timezone 設定**不一致**：

| Timezone | 機台 |
|---|---|
| **TST (UTC-? local)** | M01, M02, M04, M05, M06, M07, M08, M12, M13 |
| **JST (TST + 1 hour)** | **M03, M09, M10, M11, M14, M15, M16, M17, M18** |

DB 內 `state_transitions.timestamp` 已經被鑽孔端**正規化為 server time**（`config/machines.json` 的 `tx1_tz_offset_hours` 設定生效）。

**但 5/9 健檢時這個正規化只在 config 層修了，運行時驗證仍 pending**（drill 側 TODO）。

**MES 端對策**：
- 預設可以信任 DB timestamp 已對齊 server time
- **但建議第一次 JOIN 時抽 M14 / M16 等 JST 機台做 ground truth 比對**（拿 MES 報工的 WO 時間對 drill `state_transitions.timestamp`，差距應 < 5 分鐘）
- 對不上就**找鑽孔端反映**，可能 normalization 還沒生效

### 4.3 ⚠️ 機台時鐘漂移 ±幾分鐘

**現象**：控制電腦離線（沒網路、沒 NTP），各機台時鐘獨立飄。實測**漂移可達 ±幾分鐘**。

**MES 端對策**：
- JOIN 時用**時間區間 ±10 分鐘容忍**，不要做精確時點比對
- 例：MES 說「14:00 上 M16」，去 drill 找 13:50–14:10 之間有沒有 LoadProgram / state change

### 4.4 ⚠️ TX1 Flush Latency ~10 min

**現象**：鑽孔機內部產生事件後，TX1.Log 透過 SMB share 出來給 drill 端讀，有「buffer flush 延遲」— 大約 10 分鐘等級。drill 端 parser cycle 也是 5-10 分鐘一次。

**端到端延遲**：機台事件發生 → drill DB 看得到 → push 出去 = **約 10-20 分鐘**。

**MES 端對策**：
- snapshot 截止時間後 20 分鐘內的事件**可能不完整**
- 想要 "real-time" 看當下狀況看 `machine_current_state`（單行 snapshot，較快）；想要事件流就接受 10-20min latency

### 4.5 ⚠️ 併版（多 WO 同檯）攤分問題

**現象**：操作員會合併多張 WD 進同一個 O100.txt 一起鑽，省 setup 時間。例：M16 一個 batch 鑽 3 張 WO 共 50,000 孔、40 分鐘 RUN。

**怎麼偵測**：`o100_snapshots.active_subs` JSON array **跨多個百位範圍**。
- `[127, 128, 102]` → 全在 100-199 → **單一 WO**
- `[127, 228, 304]` → 跨 100/200/300 百位 → **併版 3 張 WO**

**MES 端對策**（5/11 drill 側決策推薦方案 C）：
- 偵測到併版 → batch 區間內**不拆分** hole_count / run_seconds 給個別 WO
- 報表標記為「`[WO-A, WO-B, WO-C] 合併批次，無法個別歸屬`」
- 若硬要拆，建議按 MES 預計點數比例攤分，並標 `confidence=low`

### 4.6 ⚠️ 機鑽 hole_count 是「小時 bucket」不是「WO bucket」

**現象**：`hourly_utilization.hole_count` 按 `(machine, date, hour)` 主鍵彙總。當 WO 跨小時或多 WO 同小時時：

```
14:00-14:30  跑 WO-A
14:30-15:00  跑 WO-B
hourly_utilization 只記得「14 點這個小時鑽了 5,000 孔」，不知道誰幾孔
```

**MES 端對策**：
- 對「WO 鑽幾孔」這個問題：
  - 整小時都是同一 WO → 直接用 `hourly_utilization.hole_count` (高信心)
  - WO 跨多小時 → SUM 跨越的小時，邊界小時要打折 (中信心)
  - 多 WO 共享同小時 → **無法精確攤分**（低信心，或標記 `不可歸屬`）
- 雷鑽沒這問題（`laser_work_orders.hole_count` 已經是 per-WO）

### 4.7 ℹ️ 週末活躍模式

**現象**：原本工廠 Mon-Fri，但 2026 年 5 月起週末也在生產（5/2-5/3 連續週末 88.9% / 79.8% 稼動率）。

**MES 端對策**：稼動率彙總**不要預設排除週末**。需要區分平假日時，改用「實際是否有生產活動」判斷，不要看日曆。

### 4.8 ℹ️ 5 月初 Phantom Hole Spikes（已修，歷史可能殘留）

**現象**：5/5-5/7 跳電前後有 4 筆 hourly_utilization 記錄 `hole_count` 異常高（154M-178M / 小時，正常上限約 50,000）。`run_seconds=0` 但孔數爆量 = counter 重讀 bug。

**狀態**：5/9 已 SQL UPDATE 歸零修復。**新 snapshot 應該不再有**這種 phantom record，但若 MES 拿到舊 snapshot 看到 `hole_count > 100,000 / hour` 直接當異常剔除。

### 4.9 ℹ️ Laser L1 沒 hole_count

**現象**：雷鑽 L1 廠商未開 INFO share，`laser_work_orders.hole_count` 永遠 NULL/0。L2-L4 正常。

**MES 端對策**：L1 的 WO 只能算 duration，不能算孔數效率。

---

## 5. 推薦 JOIN 流程（給機鑽）

### Step 1: 用 MES WO 上機時間定錨點

```
For each WO in MES list:
    expected_machine = MES.station_route
    expected_start   = MES.start_time
    expected_end     = MES.end_time
```

### Step 2: 在 drill DB 找對應時間區間的活動

```sql
-- 在 [expected_start - 10min, expected_end + 10min] 內找
SELECT timestamp, to_state FROM state_transitions
WHERE machine_id = ?
  AND timestamp BETWEEN ? AND ?
ORDER BY timestamp;

SELECT captured_at, active_subs FROM o100_snapshots
WHERE machine_id = ?
  AND captured_at BETWEEN ? AND ?;
```

### Step 3: 驗證 WO 號對得起來

- 比對 `o100_snapshots.active_subs` 百位數是否符合 WO 編號
- 比對 `machine_current_state.work_order`（如果 push 當下還在跑）
- 不一致 → 標 `confidence=low` 或「待人工核對」

### Step 4: 算 run_seconds（見 §3.2 SQL）

### Step 5: 算 hole_count（看 §4.6 分情境處理）

### Step 6: 標記 confidence

| 條件 | confidence |
|---|---|
| WO 整段在單一小時內、無併版、active_subs 對得起來 | high |
| WO 跨多小時、無併版 | medium |
| 併版偵測到 | low（或「不可歸屬」） |
| 時間區間內沒任何 RUN event | none（疑似沒上機？）|
| timestamp 對不上 ±10min 容忍 | review（可能 TZ 沒對齊） |

---

## 6. 機台速查表

| Machine | Type | 特殊 caveat |
|---|---|---|
| M01-M02, M04-M08, M12, M13 | 機鑽 | TZ=TST |
| M03, M09, M10, M11, M14-M18 | 機鑽 | **TZ=JST**（drill 端已 +1h 正規化，但運行驗證 pending）|
| L1 | 雷鑽 | hole_count 永遠 NULL（INFO share 未開）|
| L2, L3, L4 | 雷鑽 | 完整資料 |

---

## 7. 如果 JOIN 太痛苦，可以請鑽孔端做的事

目前策略是「raw push + MES 自己 JOIN」，省下鑽孔端建 WO 表的 1.5-2 週工程。但若實作後發現以下情況，請回報鑽孔端：

| 痛點 | 鑽孔端可以做 |
|---|---|
| 每次 JOIN 都要寫一樣的 CTE / window function | 建 SQL VIEW 或物化表 `takeuchi_work_orders` |
| 信心度判斷邏輯複雜重複 | 在鑽孔端標 confidence flag |
| 時區對不上 | 重跑 timezone backfill |
| 併版偵測想要 server 端做 | 加 `is_concurrent_batch` flag 到 o100_snapshots |
| 跨日 WO 邊界 | 加 WO start/end 物化 |

→ 第一個月先用 raw JOIN，**收集痛點**，再決定要不要回頭做 `takeuchi_work_orders`。

---

## 8. 推送相關

- **推送協議**：HTTP POST 到 `http://192.168.2.211:8081/upload/<filename>`（spec: `notes/drill_push_dev_spec.md`）
- **推送方式**：目前手動觸發（`tools/mes_push_db.py`），未來可能加 daily auto-push（待評估必要性）
- **檔名格式**：`drill_snapshot_YYYY-MM-DD_HHMM[_label].db.gz`
- **頻率**：目前按需推送（「補齊」用）

---

## 9. 聯絡 + 版本

- **鑽孔端窗口**：Ryan (shihpohsu@gmail.com)
- **鑽孔端 spec**：`notes/drill_push_dev_spec.md`
- **鑽孔端整合規劃**：`notes/mes_integration_plan.md`
- **本文件版本**：2026-05-12（初版，隨 push pipeline 上線同步）

**回報機制**：MES 端用 raw JOIN 跑 1-2 週後，請回報：
- 哪些 WO 對得起來、對不起來、信心度分布
- 痛點最大的 query pattern
- 想要鑽孔端加什麼欄位 / 表 / index

依此決定下一步要不要做 `takeuchi_work_orders` 物化表。

# TX1.Log Flush 延遲調查計畫

**調查日期：** 2026-04-19（規劃中）
**實際測試預計：** 2026-04-23 起（18 台開測環境就緒後）
**相關個案：** M13 2026-04-18 10:33 LoadProgram `O2502016.T1` 延遲 77 分鐘才被 parser 偵測到

---

## ⚠️ 2026-04-23 重大修正（務必先讀）

用 4/22–4/23 生產環境 DB snapshot（18 台、18 小時、`log_file_observe` 11340 筆、`tx1_event_latency` 1373 筆）重新分析，**前面部分結論站不住**：

### 發現 1：SMB mtime 不可靠（M01/M03/M17 三台）

Drive.Log 在這三台機器上，**mtime 可以卡 10 小時不動**（整天開著 write handle，Windows SMB lazy update），但 `file_size` 每 10 分鐘都在長。這是 Windows SMB 本身的行為，不是機台韌體問題。

→ **任何用 mtime 判斷「有沒有寫入」的觀察都要重驗。** 改用 `file_size` 才可靠。

### 發現 2：「TX1 穩定 RUN 中期 30+ 分鐘不 flush」疑為 mtime 假象

4/20 測試觀察到的 29.7 分鐘 gap 是 **mtime** 上的 gap。今天分析顯示 TX1 在 M01/M03/M17 也有 lazy-mtime 現象（雖然比 Drive 輕微）。所以「buffer 累積」可能部分是 mtime 沒刷新，不是真的沒 flush。

**content probe 測得的 157s / 164s / 160s 延遲仍然有效**（那是直接讀檔內容，不受 mtime 影響）。

### 發現 3：「一般延遲 5–8 分鐘、最壞 30+ 分鐘」是 backfill 假象

`tx1_event_latency` 全量看起來很糟（p50 ~24400s、max 40 小時），但**那是系統初次啟動時 parser 掃到 TX1.Log 裡幾個月的歷史事件**，event_ts 是舊的，detected_at 是現在，算出來的 delay 不是真實 flush 延遲。

篩選 `event_ts >= '2026-04-22 18:00'`（系統啟動後才發生的事件）後：

| 延遲區間 | 事件數 |
|---|---|
| < 1 min | 3 |
| 1–5 min | 88 |
| 5–10 min | 129 |
| 10–30 min | 22 |
| **> 30 min** | **0** |

**最大延遲 689s ≈ 11.5 分鐘**，p50 集中 5–10 分鐘（= 等半個 parser cycle）。**完全沒有 30+ 分鐘延遲的證據。**

### 修正後的真實延遲模型

```
機台事件 → log 檔（~即時，可由 size 推進驗證）
        → parser poll cycle（0–10 分鐘，由 poll_interval_seconds=600 決定）
        → DB（即時）
        → 前端 refresh（0–10 分鐘）
        → dashboard 顯示
        
end-to-end 最大 ≈ 20 分鐘，完全由 cycle 週期決定，非 flush 延遲。
```

### 對 Layer 2（transition-triggered parse）的影響

原本動機「把 flush 等候 + buffer 累積壓到 <5 min」**站不住** —— 因為那個 buffer 累積本身是 mtime 假象。如果要做 Layer 2，需要換理由（例如直接做「縮短 parser cycle」或「縮短前端 refresh」可能更便宜）。見 [tx1_transition_triggered_parse_plan.md](tx1_transition_triggered_parse_plan.md) 同步更新。

### 以下原文保留作歷史記錄

（4/19–4/20 的假設、實驗設計、測試結果保留，因為實驗方法論本身仍有價值；但其中根據 mtime 得出的結論請對照上述修正讀。）

---

## 背景

Takeuchi 控制電腦 `{DD}TX1.Log`（OpeLog）事件從機台內部發生到 SMB reader 可見之間有延遲，本次個案觀察到 77 分鐘。已排除 parser 邏輯錯誤，延遲源在「控制電腦端」（機台韌體 buffer / Windows 檔案系統 cache / SMB server 快取之一），運算端無法直接修正。

**影響範圍（已釐清）**：
- 機台層級 KPI（稼動率、state、counter、transitions）不受影響 → go-live 2026-04-20 機台稼動率報表可靠
- 工單層級（per-WO 工時、per-WO 孔數、per-WO 成本、操作員績效）在延遲視窗內會誤歸屬 → 未來 MES 整合 / 模擬排程受影響

**為什麼要調查延遲分佈**：模擬排程需要用歷史 per-WO 時間和孔數訓練速率模型。誤差大小取決於延遲是罕見極端值還是常態，目前樣本數 = 1 個事件，無法判斷。

---

## 待驗證的假設

### 假設 H1：操作員開啟 TX1.Log（例如 Notepad）導致機台寫入被 buffer

**證據**：2026-04-18 個案的照片顯示控制電腦上有 `18TX1.Log - メモ帳`。若操作員 10:50 開 Notepad 持續到 11:50 附近才關，就能解釋這段 77 分鐘延遲。

**預測**：開檔期間 `os.stat()` 看到的 `file_size` 和 `mtime` 凍結，關檔後一次跳一大段。

### 假設 H2：延遲是機台韌體 time-based flush（例如每 N 分鐘或每 N 行才 flush）

**預測**：在無人開檔的時段也會觀察到週期性的「size 凍結 → 跳大段」模式。

### 假設 H3：延遲跟機台狀態相關（例如 idle 時不 flush，busy 時常 flush；或相反）

**預測**：RUN 和 STOP/RESET 期間的延遲分佈有顯著差異。

### 假設 H4：18 台機的延遲行為不一致

**預測**：不同機台延遲分佈差異明顯，可能跟機台年份、韌體版本、使用習慣相關。

---

## 調查方法（兩路並進）

### 方法 A：小型受控實驗（驗證 H1）

**設計**：挑一台有穩定生產的機台（例如 M13）進行 45 分鐘實驗。

**時程**：
| 分鐘 | 動作 |
|---|---|
| 0:00 ~ 5:00 | **開啟** Notepad 於 `{DD}TX1.Log`（控制電腦上）|
| 5:00 ~ 15:00 | **關閉** Notepad |
| 15:00 ~ 20:00 | **開啟** Notepad |
| 20:00 ~ 30:00 | **關閉** Notepad |
| 30:00 ~ 35:00 | **開啟** Notepad |
| 35:00 ~ 45:00 | **關閉** Notepad |

**前提**：
- 實驗期間機台為 RUN（AUTO 狀態），確保有持續事件寫入
- 精確記錄每次開/關的時間戳（拍照或寫紙條）

**觀察指標**：運算端每 10~30 秒 `os.stat()` TX1.Log，記錄 `file_size` 和 `mtime`。

**判讀**：
- H1 成立：「開」區間 size 凍結，「關」區間開始不久 size 跳大段（追寫）
- H1 不成立：size 穩定成長、與開/關狀態無關

**風險 / 限制**：
- 若 FILEOPERATION 事件太稀疏，無法單靠事件判讀；主要看 size/mtime 曲線
- 實驗中操作員要配合，不能生產中打亂節奏

### 方法 B：長期被動觀測 + 跨 log 交叉比對（驗證 H2/H3/H4）

**時程**：4/23 18 台環境就緒後，部署 instrument，連續跑 **至少 2 週**。

**Instrument 設計**：

1. **擴充 robocopy 收集範圍** — 除了現行的 Drive.Log / TX1.Log，再加其他 3 種 log（確切檔名到現場才能列；目前已知共 5 種）。
   - 重點：不 parse，只 stat，用來做跨 log 比對
   - 修改點：[collector/log_collector.py:115-122](collector/log_collector.py#L115-L122)（加新檔名到 robocopy 指令）

2. **新增 2 張觀察表**：
   ```sql
   -- 每輪 cycle 每個 log 檔案的 size/mtime 快照
   CREATE TABLE log_file_observe (
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       machine_id TEXT NOT NULL,
       log_type TEXT NOT NULL,     -- 'TX1', 'Drive', 其他
       observed_at TEXT NOT NULL,  -- server 時間
       file_size INTEGER,
       file_mtime TEXT,
       smb_age_sec REAL            -- observed_at - file_mtime
   );

   -- 每個 FILEOPERATION LOAD 事件的延遲
   CREATE TABLE tx1_event_latency (
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       machine_id TEXT NOT NULL,
       event_ts TEXT NOT NULL,     -- log 行時間戳
       detected_at TEXT NOT NULL,  -- 首次被 parser 讀到
       delay_seconds REAL NOT NULL,
       program_name TEXT,
       wo_matched INTEGER,         -- 0/1
       UNIQUE(machine_id, event_ts, program_name)
   );
   ```

3. **修改點**：
   - [parsers/tx1_log_parser.py:104-133](parsers/tx1_log_parser.py#L104-L133) — 每個 FILEOPERATION 事件 INSERT OR IGNORE 到 `tx1_event_latency`
   - 每輪 parse cycle 開始時，`os.stat()` 所有觀察中的 log 檔，寫入 `log_file_observe`
   - 不影響現有 parse 邏輯

4. **請操作員配合記錄**（手寫表單）：
   - 什麼時候開了 TX1.Log（或其他任何 log）在控制電腦上看
   - 幾點關
   - 事後整理成 CSV 餵進分析

### 方法 C：跨 log 交叉比對（驗證 H1 的更強證據）

假設 5 種 log 檔彼此獨立，而操作員通常只會開其中 1 種（例如 TX1），那麼：

| 觀察到 | 結論 |
|---|---|
| TX1 size 凍結、其他 4 種照常成長 | H1（Notepad 假設）強支持 |
| 5 種同時凍結 | 整台控制電腦或 SMB 層級問題，非 Notepad |
| 只有被開的那種凍結（跨不同實驗日期驗證）| H1 確認 |
| 凍結規律跟開檔無關 | H1 推翻，H2 更可能 |

這個比對方法的**關鍵前提**是「操作員不會同時開多種 log」—— 需要 4/23 現場確認實際操作習慣。

---

## 分析產出

兩週資料累積後跑的分析：

```sql
-- 每台機器延遲分佈
SELECT machine_id,
       COUNT(*) n,
       MIN(delay_seconds) min,
       AVG(delay_seconds) avg,
       MAX(delay_seconds) max
FROM tx1_event_latency
GROUP BY machine_id;

-- 跟時段的相關（白班 / 夜班）
SELECT strftime('%H', detected_at) hour,
       AVG(delay_seconds) avg_delay,
       COUNT(*) n
FROM tx1_event_latency
GROUP BY hour
ORDER BY hour;

-- 跟機台 state 的相關（JOIN state_transitions）
-- 伪 SQL：查每個事件發生瞬間機台是什麼狀態
```

**產出**：
1. 延遲分佈 histogram（p50/p90/p99）
2. Notepad 假設驗證（實驗 A 結果 + 跨 log 比對）
3. 18 台機之間的差異圖
4. 建議的「延遲 buffer」設定值（模擬排程時工單時間要加多少 padding）

---

## 對後續階段的意義

**4/20 go-live**：不影響。機台層級 KPI 不依賴此資料。

**未來 MES 整合 / 模擬排程**：
- 若分佈顯示延遲 p95 < 10 分鐘 → 對 8 小時工單影響 <2%，可直接用
- 若 p95 ~ 1 小時、極端值幾小時 → 需要雙來源交叉校驗（MES 也不準，見下）或其他緩解
- 若 H1 成立 → 可以宣導操作員不要開 TX1.Log（SOP），根本解決

**MES punch-in 也不準**（操作員忘按/補按），不能當唯一 ground truth。最終工單切換點可能需要「MES punch + 機台 state 轉態 + TX1 work_order 三者交叉」取最佳猜測。

---

## Action Items

- [x] **4/19**：寫 instrument code（2 張表、flush_observer 模組、parser 加 insert、Mac dev 驗 schema migration）
- [x] **4/19**：確認 log 檔共 6 種（Drive / TX1 / MACRO / TARN / FILE / Alarm）
- [x] **4/19**：寫兩個 probe 工具（flush_probe.py 檔案 metadata、flush_probe_content.py 行級 first-seen）
- [x] **4/20**：M13 執行初步受控測試（因工號提早完成只跑了 2 cycle，見下方結果）
- [ ] **4/23 起**：18 台環境就緒後，長期被動觀測（instrument 已在 production 跑）
- [ ] **4/23 之後某次長工單**：補做受控測試（每 cycle open 20 分鐘、3 cycle，跨過 burst gap）
- [ ] **兩週後**：用 analyze_flush_latency.py 分析累積資料，寫結論

---

## 4/20 測試結果（M13，受控但縮短版）

**實際執行時間**：13:07 開始，13:41:39 機台停止，總共 34 分鐘（原訂 2 小時）。

**實際 phase**：
- 13:07-13:24 Phase A baseline（17 min）
- 13:24-13:27 B1 open（3 min，原訂 5 min）
- 13:27-13:37 B1 closed（10 min）
- 13:37-13:41 B2 open（4 min）
- 13:41 機台停止（工號跑完，未做 B2 closed 及後續 phase）

**收到的樣本數**（drill_monitor_test_20260420_v2.db + 2 支 probe CSV）：
- tx1_event_latency：9 筆（8 筆為 catch-up，1 筆為測試期間真實事件）
- log_file_observe：96 筆（16 cycle × 2 機 × 6 log 類型的一部分）
- state_transitions：M13 當日 13:00-14:00 共 8 筆
- probe_file_M13.csv：120 筆 TX1 + 120 筆 Drive
- probe_content_M13.csv：1 筆新事件（13:07:04 LoadProgram O100.txt）

### 關鍵發現 1：Drive.Log 完全不受影響（對照組成立）

120 次取樣每次都有 delta、0 次凍結；全場成長 ~450KB。SMB 管道沒問題，問題在 TX1.Log 本身。

### 關鍵發現 2：TX1.Log 是「burst 式 flush」，不是線性

整個測試 TX1.Log mtime 只在這些時段有更新：

| mtime burst 期間 | 相對應的 state 轉態 |
|---|---|
| 13:06:59 ~ 13:09:09 | 13:07:49, 13:07:50, 13:08:16, 13:08:23（4 次轉態）|
| 13:12:27 ~ 13:24:33（**12 分鐘 gap**）| （無轉態）|
| 13:24:33 ~ 13:26:13 | 13:26:08, 13:26:13（RUN→STOP→RUN）|
| 13:26:13 ~ 13:36:57（**10.5 分鐘 gap**）| （無轉態）|
| 13:36:57 ~ 13:41:31 | 13:41:31（RUN→STOP）|

**推論**：機台韌體在 state 穩定時把 OpeLog 攢在記憶體，state 轉態（或同類的「事件」）才觸發 buffer dump。這就是「TX1.Log 在穩定 RUN 時 10+ 分鐘不動」的原因。不是 bug、不是 SMB 問題、是機台設計。

### 關鍵發現 3：Notepad 假設（H1）這次沒得到支持

- Phase A baseline 也出現了 12 分鐘長 gap（無人開 Notepad）
- B1 open 期間 TX1 file size 在觀察端看似凍結，但 mtime 顯示 13:24:33 其實有更新；觀察端 13:27:03 才看到 size 改變（Notepad 關檔後 3 秒）
- B2 open 期間 TX1 file 邊開邊 flush（13:39:33 size 就更新，Notepad 仍開著）
- **結論：這次資料不能證明 Notepad 單獨造成延遲**。但由於 open 時間太短（3-4 min < burst gap 典型 10-15 min），可能剛好夾在兩次 burst 間而無法顯示差異

### 關鍵發現 4：實際延遲比 4/18 小很多

- 唯一測試期間產生的事件（13:07:04 LoadProgram）延遲 **157 秒（content probe 測量）**
- 4/18 極端個案的 77 分鐘沒有重現
- 可能解釋：4/18 當天操作員 Notepad 開了遠超過 3-4 min（照片只能證明「開過」，不知長度）、或 4/18 那段時間機台狀態穩定太久沒觸發 burst

### 測試限制（誠實記錄）

1. 只有 2 cycle、open 時間太短（3-4 min）→ 落在 burst gap 內觀察不到對照
2. 工號只有一個，中間沒 LOAD 事件 → content probe 只 1 筆新事件
3. 無法重現 4/18 症狀
4. 下次測試要每 cycle ≥20 min、3 cycle、配合長工單

### 對專案的意義（修正後）

- **2-3 分鐘是延遲常態**、**10-15 分鐘 burst gap 是穩定 RUN 時的正常現象**
- 77 分鐘目前僅 1 個樣本、是否會重現未知
- 機台稼動率 KPI（go-live 主 KPI）**完全不受影響**
- 未來模擬排程：對長工單（>2h）影響 <12%；短工單或頻繁切換要加 buffer
- 可能的優化方向（未實作）：偵測到 state 轉態時強制跑一次 TX1 parse（因為轉態時剛好 flush，抓工單切換最即時）

### 測試檔案位置

- `/Users/ryanhsu/Documents/drill_mes/original_logs/verify/drill_monitor_test_20260420_v2.db`
- `/Users/ryanhsu/Documents/drill_mes/original_logs/verify/probe_file_M13.csv`
- `/Users/ryanhsu/Documents/drill_mes/original_logs/verify/probe_content_M13.csv`

---

## 4/20 第二次測試結果（state duration 假設驗證）

**執行時程**（M13，長短工單交錯）：
- 14:40-15:24 工單 A `O2604058.T`（**44 min 長 RUN**）
- 15:26-15:35 工單 B `O2604055.T1`（9 min 短 RUN）
- 15:36-15:48 工單 C `O2604069.T`（12 min 短 RUN）

**假設驗證結果：**

| 假設 | 預測 | 觀察 | 結論 |
|---|---|---|---|
| 連續 RUN 越久、gap 越長 | WO A 中會有 10+ min gap | **觀察到 29.7 min gap**（14:55:07 → 15:24:49）| ✅ 確認 |
| 工單切換立即觸發 flush | 切換時 mtime 跳動 | 15:24:36 RUN→STOP → 13 秒後 TX1 mtime=15:24:49 | ✅ 確認 |
| 短工單 flush 較頻繁 | B、C 的 mtime 更新多 | 每分鐘成長 bytes：A=72 / B=270 / C=429 | ✅ 確認 |

**延遲模型（修正後，三組件）**：

```
事件發生 → [buffer 等候 0~30+min] → [SMB 傳播 ~2-3min] → [parser cycle 0-10min]
            ^                         ^                      ^
            穩定 RUN 越久越長           穩定                   poll_interval 決定
```

- 一般情況（轉態附近事件）：5-8 分鐘總延遲
- 最壞實測（穩定 RUN 中段）：29.7 分鐘 buffer gap
- 理論最壞（長於 1 小時穩定 RUN）：~60 分鐘（解釋 4/18 的 77min）

**4/20 測試的事件延遲明細（content probe + DB 一致）**：
- 15:25:22 LOAD → 164.3s 延遲
- 15:35:57 LOAD → 160.0s 延遲

**工單 A 內部 mtime 更新時序**：
```
14:38:55 → 14:39:07 → 14:40:33 → 14:42:25 → 14:43:11   （密集：start-up 轉態）
                                          ↓
                                    14:55:07   （稀有中段 flush）
                                          ↓
                                          （29.7 分鐘 gap ⚠️）
                                          ↓
                                    15:24:49   （RUN→STOP 觸發）
```

### 測試檔案位置（第二次）

- `/Users/ryanhsu/Documents/drill_mes/original_logs/verify/drill_monitor_test_20260420_statedur.db`
- `/Users/ryanhsu/Documents/drill_mes/original_logs/verify/probe_file_M13_st.csv`
- `/Users/ryanhsu/Documents/drill_mes/original_logs/verify/probe_content_M13_st.csv`

---

## 結論總結（兩次測試後）

1. **TX1.Log flush 是 state 轉態驅動**，不是時間驅動
2. **穩定 RUN 中期 buffer 可累積 30+ 分鐘**（觀察上限）
3. **工單切換必觸發 flush**（13 秒內 mtime 更新）
4. **基礎 SMB 延遲 2-3 分鐘穩定**
5. **Notepad 假設 H1 未被證實**，4/18 的 77min 主因改推測為「穩定 RUN buffer 累積」

## 可優化方向（尚未實作）

**優化：state 轉態觸發 TX1 parse**

在 `drive_log_parser.py` 偵測到 `RUN→STOP`（工單結束）或 `RESET→STOP/STOP→RUN`（工單切換）時，立刻觸發一次 robocopy + tx1_log_parser。因為 state 轉態幾乎必定觸發 TX1 flush，此時讀 SMB 就能拿到新工單號。

**預期效果**：工單切換偵測延遲從「0~ 40分鐘（取決於 parser cycle 週期 + buffer 狀態）」壓到 「<5 分鐘（SMB 傳播延遲為主）」。

實作位置：`parsers/drive_log_parser.py` 的 state transition 偵測處 + 新增 `tx1_log_parser.parse_tx1_for_machine(machine_id)` 單機 API。

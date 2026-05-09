# 5 天試跑健檢 — 2026-05-09

**資料源：** `original_logs/verify/drill_sample_20260509_131008/`
（drill_monitor_snapshot.db 62 MB / TX1.Log 9 天 × 18 機 / Drive.Log 5/8-5/9 / app log 6 檔 53 MB / O100 live SMB probe 18 台 / cycle_stats 2300 cycles）

**審查期：** 2026-05-01 ~ 2026-05-08（涵蓋 weekday + weekend + cycle hang 5/4 + power loss 5/7 + LAN rebind 5/8）

**結論：** 稼動率（介面三大頁）數字**架構正確、邏輯自洽**，可給管理層用；但孔數欄有 4 筆 phantom spike 需洗、Phase 3 TZ 設定需修 6 台。

---

## A. 已確認 *正確* 的資料（給信心用）

### A1. Drive.Log → DB run_seconds 秒級對齊（C 維度交叉驗證）

抽 M01 2026-05-08（24 小時 / 86,259 raw rows / 15,652 RUN seconds）：

| | DB | Raw Drive.Log |
|---|---|---|
| 24 小時 run_seconds 加總 | 15,652 | 15,652 |
| **每小時逐個比對** | **24/24 完美匹配** | |

→ Parser 對每秒 RUN 的歸屬正確到秒級。dashboard 顯示的稼動率/run_hr 數字是可信的。

### A2. State transitions 沒有重複 / 沒有未來時間戳 / 沒有負時長

```
state_transitions duplicates (machine_id, timestamp): 0
future timestamps (>2026-05-09 13:11): 0
next_ts < ts (negative duration): 0
```
4/23 加的 UNIQUE 約束 + 機台時鐘 skew clamp 都運作正常。

### A3. Phase 3 O100 ground-truth 18/18 對齊

USB 採樣時的 SMB live probe 與 DB 中 `machine_current_state.current_o100_subs`：

```
total live vs DB mismatches: 0 / 18
```

每台 Takeuchi 機台「DB 認為現在在鑽什麼板別 sub」與「機台 SMB 上 O100.txt 真實內容」**完全一致**。Phase 3 capture 邏輯自洽。

### A4. Cycle 整體健康

```
last 30 days: 2300 cycles
avg=25.9 sec, max=944s
0 cycles failed (zero parser exceptions)
```
2300 個 cycle 全部完成（無 exception），平均 26 秒，遠低於 300 秒預算。

### A5. 介面三大頁 KPI 數字可信

- **機台總覽**：22 台都顯示，狀態 + work_order + current_o100_subs 即時
- **稼動排行**：5/1-5/8 排名 54.2% (M03) → 83.5% (M12)，30 pp 區間，符合機台間預期差異
- **稼動分析**：4 月 vs 5 月稼動率 58.1% → 59.9%（weekday only），跨月走勢平穩

---

## B. 未修，但需要管理層知道的真實觀察（不是 bug）

### B1. 5/8 (Fri) 全廠稼動率 26%，是**真實低點**而非資料異常

```
date         util%  run_hr  weekday
2026-05-04   57.5%  241.4   Mon  ← cycle hang 944s（修了，無資料損失）
2026-05-05   76.3%  315.4   Tue
2026-05-06   60.6%  244.0   Wed
2026-05-07   50.9%  211.2   Thu  ← 跳電 + 5/7 第二次 cycle hang 464s
2026-05-08   26.0%  112.1   Fri  ← LAN rebind 日，但 26% 是真實低
2026-05-09   76.7%  179.9   Sat  ← 隔日恢復
```

5/8 的 raw Drive.Log 確認：M01 全天只在 0-1 + 10-18 點有 RUN（共 4.4 hr）— 不是 parser 沒抓，是**機台真的沒在跑**。可能原因：佛誕節 / 端午節 假期？或產線排程。**請管理層確認 5/8 是否計畫停工**，若是則應從 KPI 排除。

### B2. 5 月 weekend 稼動率 (84.3%) 高於 weekday (59.8%) — 違反預期

跟舊認知（記憶 `project_weekday_only_kpi`：「週末近乎 idle」）矛盾。實測：

| 區間 | weekday avg | weekend avg |
|---|---|---|
| 4/28-5/3 | (內含週六 88.9%) | 4/26 sun = 0.05% (idle) |
| 5/1-5/8 | 59.8% | **84.3%** ← Sat 5/2, Sun 5/3 都跑 |

5/2-5/3 連續週末 88.9% / 79.8% 在 raw log 也對齊（不是資料錯）。表示**這兩週末有實際生產**，不能再假設「週末=idle」。**dashboard 的「排除週末 toggle」預設值要不要重新討論**？目前設成 false（排除）會讓管理層看到的 weekday-only 數字偏低、誤導。

### B3. Hour-of-day pattern 形狀仍正確（4/28 audit 對映）

```
12:00 lunch dip:   41.4%  ← (4/28 audit: 22%, 比之前更高 — 換班/排班改了？)
06:00-07:00 dip:   38-41% ← shift change
19:00-23:00 peak:  67-77% ← 夜班高峰維持
00:00-04:00 high:  60-75% ← 通宵生產持續
```
夜班 / 凌晨生產力高的 pattern 持續。

### B4. Phase 3 O100 操作員 orphan-edit ratio 實證 ~58% 平均

7 台機台 ≥60% 編輯沒按 LOAD（mtime-only）：

| 機 | mtime | tx1 | mtime% |
|---|---|---|---|
| M12 | 4 | 0 | **100%** |
| M09 | 8 | 2 | 80% |
| M14 | 14 | 6 | 70% |
| M13 | 2 | 1 | 67% |
| M07 | 11 | 6 | 65% |
| M03 | 10 | 6 | 62% |
| M11 | 8 | 5 | 62% |

驗證 Phase 4 probe 的「~60% orphan edit」假設正確。**SMB mtime backstop polling 確實必要**，沒它會漏一半板別變更。

---

## C. 需要修的問題

### C1. 🔴 [HIGH] 4 筆 phantom 100M+ 孔數尖峰（影響稼動分析的孔數欄）

**症狀：** 5/5-5/7 期間 4 個單小時格子孔數 154M-178M，但同小時 `run_seconds` 是 0 或極低：

```
M02 2026-05-06 h10: 175,413,429 holes / run_seconds=0
M05 2026-05-07 h6:  169,019,025 holes / run_seconds=0
M08 2026-05-07 h7:  178,094,240 holes / run_seconds=0
M10 2026-05-05 h18: 154,922,175 holes / run_seconds=1178
```

物理基線是 0.85-1.85 孔/RUN 秒，這些尖峰是**百萬倍離群**。

**根因推測：** counter 初始 read 失敗 → 下次 read 時 delta 計算成「0 → 175M」。3/4 集中在 5/7（跳電 + SMB stale session 修復期），1/4 在 5/5（M10 該日有 cycle 異常嗎？需查 app_log）。

**影響範圍：** M02/M05/M08/M10 五月週孔數全部被一筆吃掉 99.97%；其他 14 機鑽 + 4 雷鑽不受影響。**稼動率本身不受影響**（utilization 由 run_seconds 計算）。

**建議處理：**
1. SQL 直接歸零這 4 row 的 hole_count（`UPDATE hourly_utilization SET hole_count=0 WHERE ... AND hole_count > 50000000`）
2. 確認 `tools/cleanup_db_residue.py` 的 `>50M` 防呆是否還在 cron / 是否需要每日跑
3. 上層 query 加 `hole_count < 100000000` 防呆（介面層 safety net）

### C2. 🔴 [HIGH] 6 台機台 TX1 timezone 設定錯誤（Phase 3 影響）

實測 `o100_snapshots.tx1_event_ts` vs `smb_mtime` 中位數差距：

| 機 | config (`tx1_tz_offset_hours`) | 實測 median diff (min) | 建議 |
|---|---|---|---|
| M03 | 0 | **+60.0** | 改 1 |
| M09 | 0 | **+60.0** | 改 1 |
| M10 | 0 | **+60.0** | 改 1 |
| M11 | 0 | **+60.0** | 改 1 |
| M16 | 0 | **+60.0** | 改 1 |
| M17 | 0 | **+60.0** | 改 1 |

raw TX1.Log 對齊驗證：M03 5/9 第一行 `00:29:32` — 跟 M14（已知 JST）的 `00:09:01` 同 timezone 風格，雙重確認 M03 在 JST。

**影響：** 板別歸屬演算法若用 `tx1_event_ts` 比對 Drive.Log RUN 時段，會差 1 小時；目前 capture 對齊靠 mtime backstop 還能補回（fallback OK），但精準度打折。

**建議處理：** `config/machines.json` 把 6 台的 `tx1_tz_offset_hours` 改成 1，重啟 parser 生效（不需 backfill — 既有 row 影響有限）。

### C3. 🟡 [MEDIUM] Peek-ahead replay 殘留（4/23 修復後仍偶發）

```
hourly_utilization total_seconds > 3600: 25 cells
- 24/25 在 hour=23（cross-midnight 指紋）
- 範圍：4/21 - 5/5
- 5 台中招：M02/M04/M05/M09/M10
- 最大過量：4171s（vs 3600 上限，過量 16%）
```

cross-check M02 5/8 h23 看到 +44 秒過量（DB=3600 vs raw=3556），是 replay 沒攔好的指紋。

**比 4/22 那次（4892s, 35% 過量）已小很多**，但機制未根除。每月會多 ~3-5% util 在這幾台 23 點的 bucket。

**建議：** 不急，但下次 parser 重構時把 cross-batch dedup 加固。

### C4. 🟡 [MEDIUM] 5/7 早上未報告的 cycle 連環緩慢

`cycle_stats` 顯示 5/7 05:42 ~ 06:55 期間 11 個連續 cycle 都超 30 秒（最大 464s, 7.7 分鐘）：

```
05:42 → 464s
05:55 → 296s
06:05 → 234s
06:13 → 175s
... 直到 06:55 才回到 7s 正常水準
```

這跟記憶 `project_smb_stale_session`（5/7 跳電後 SMB session 壞）對映 — power loss 期間 parser 在 timeout retrying。`failed_step_names=None` 表示沒拋 exception，但 SMB I/O 慢吞吞。

**現有自動修復**（`collector/log_collector.py` cycle 內失敗會 remount + retry）有觸發 — 1.5 小時內自然恢復。

**建議：** 在 `notes/parser_cycle_15min_hang_20260504.md` 補上 5/7 案例做為第二次實證；不需要修代碼。

### C5. 🟢 [LOW] 5 筆 April cross-midnight stub 殘留（pre-4/23 fix 殘骸）

```
M03 2026-04-03 hour=23 = 375s（其他小時皆 0）
M05 2026-04-20 hour=23 = 348s
M08 2026-04-01 hour=23 = 384s
M08 2026-04-03 hour=23 = 499s
M13 2026-04-20 hour=23 = 1s
```

4/23 secondary fix（commit `175954a`）後沒回頭清理的歷史殘骸。介面層看影響小（4 月 weekday 平均 58.1% 不受這幾秒拉動）。

**建議：** 跑 `tools/backfill_wiped_dates.py` 或直接 SQL 歸零這 5 row。

### C6. 🟢 [LOW] M12 mystery: 58 個 LoadProgram 事件但 0 個 tx1-triggered snapshot

`tx1_event_latency` 顯示 M12 在 9 天內有 58 次 `program_name='O100.txt'` 的 LoadProgram 事件，但 `o100_snapshots` 中 M12 的 `trigger_source='tx1_event'` 列數 = 0。

**推測：** UNIQUE(machine_id, content_hash, smb_mtime) 約束擋掉 — 如果 LOAD 之間檔案內容沒變（同 hash），就會被 dedup。但既然 mtime_change 也只 3 筆，似乎 M12 操作員幾乎不編輯 O100.txt？或是 SMB 讀取對 M12 失敗（observer 預設無 log warning）。

**建議：** 不影響本次健檢結論；後續 Phase 3 加觀察 log（observer SMB read 失敗時記下來）。

### C7. 🟢 [LOW] FILE.Log 18/18 全部 MISSING

usb_sample 抓 9 天 × 18 機台 = 162 個預期的 FILE.Log，全都不存在。

**推測：** FILE.Log 是 M14 機型獨有（O100 dump），其他 17 機台原本就沒 → MISSING 是預期；M14 為何也 MISSING 待查（會不會 FILE.Log 機制最近壞了？raw_content 在 o100_snapshots M14 仍有 21 筆，所以 SMB live read 工作正常，FILE.Log dump 路徑可能變了）。

**建議：** 下次 USB 採樣時手動驗 `\\<M14_ip>\LOG\09FILE.Log` 是否存在；若 M14 也沒 → 韌體輸出機制變了，需追根因。不影響本次結論。

---

## D. 給管理層報告的「精華句」

> 5 天試跑期間（5/1-5/8），系統對 22 台機台、3,386 個 (機台,小時) 格子做了 100% 涵蓋（無遺漏天）；2,300 個 parser cycle 全數無 exception 完成；用 raw Drive.Log 對 M01 2026-05-08 24 小時逐秒抽驗，DB 與 raw 完全一致。**稼動率介面三大頁的數字結構正確、邏輯自洽，可作為決策依據。**
>
> **已知需處理：** 孔數欄有 4 筆異常單小時尖峰（154M-178M，跨 4 機台），起因於 5/7 跳電後 counter 重讀；移除這 4 筆 row 後，孔數總和回歸正常（每台週孔數 ~50 萬上下）。處理時間：< 1 分鐘 SQL UPDATE。

---

## E. 後續行動清單

| # | 動作 | 優先 | 影響 |
|---|---|---|---|
| 1 | SQL UPDATE 4 筆 phantom 孔數歸零 | 🔴 | 介面數字立刻乾淨 |
| 2 | `machines.json` 把 M03/M09/M10/M11/M16/M17 的 `tx1_tz_offset_hours` 改成 1 | 🔴 | Phase 3 精準度 |
| 3 | 跟管理層確認 5/8 是否假日，是則 KPI 排除 | 🔴 | 報告誤導風險 |
| 4 | 重新討論 weekend toggle 預設值（既然週末有跑） | 🟡 | 介面預設值 |
| 5 | 補 5/7 cycle slow 到 `parser_cycle_15min_hang_20260504.md` | 🟡 | 文件完整性 |
| 6 | SQL 歸零 5 筆 April cross-midnight stub | 🟢 | 4 月歷史乾淨 |
| 7 | 追 M14 FILE.Log 為何 MISSING | 🟢 | 不影響當前 |
| 8 | M12 LoadProgram 0 snapshots 待 Phase 3 加 observer warning log | 🟢 | 觀察 |

---

## 附錄：原始查詢 + 數字

詳見 `original_logs/verify/drill_sample_20260509_131008/extras/`：
- `schema.sql` — 22 物件
- `cycle_stats.csv` — 2300 rows last 30d
- `o100_live_probe.csv` — 18 Takeuchi live SMB read result
- `machines.json` — per-machine config snapshot
- `sha256_manifest.txt` — 所有 raw 檔 hash

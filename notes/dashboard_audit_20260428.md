# Dashboard 資料稽核 — 2026-04-28

跨檢資料：USB 快照 `original_logs/verify/drill_sample_20260428_154529/`
（DB snapshot 16 MB + 18 台 Drive.Log/TX1.Log + 4 台 ClsPLCTrd + app log）

## 已修復項目（commit 已落）

- `tools/cleanup.py` 不再砍 Kataoka `programs/` `lsr_files/` 目錄（regex 限定 `^\d{8}$`）
- `DATA_START_DATE = "2026-04-01"` 把 3 月測試殘渣從所有彙總 API 屏蔽
- 月視圖週標籤改用月曆 W1-W5（含空週），不再用「有資料的週」順序編號
- 排行榜 KPI 機鑽/雷鑽 跟著時段切換連動
- 排行榜加 月/週/日 toggle
- L1（skip_info）孔數欄顯示「無孔數」斜體灰，不顯示 0
- DB cleanup 腳本（`tools/cleanup_db_residue.py`）：217 row March 殘渣 + M03/M07 outliers + 防呆 hole_count > 50M
- 週末包含/排除 toggle（本次 commit）
- 雷鑽 per-hour hole_count 改 beam-event 分佈（本次 commit）

## 未修，僅記錄的觀察

### 1. M14 / M18 帶舊月 work order

`machine_current_state` 中 M14 與 M18 顯示 `work_order = O2603035-2`（3 月前綴），
其他機台都是 `O2604xxx`。可能是 work order 切換偵測有 edge case，或是真的還在
跑跨月延續單。下次稼核時實機看一眼即可，不影響稼動率計算。

### 2. L1 cross-midnight 100% RUN 的真實性

App log 有寫 `[L1] Cross-midnight RUN carryover: 00:00:00 -> 09:15:37`。
驗證 raw ClsPLCTrd：

- 00:00 - 09:15 期間有 **3,916 筆 beam events**
- 最後一筆 beam at 08:46:44，09:15 是 ClsLaserCom 的 DEL 訊號
- 信號層面 RUN 為真，但 hole_count = 0（skip_info → 無 work order → 無歸戶）

當廠商把 INFO share 建立後，去掉 L1 的 `skip_info`，hole_count 會自動補回。
不必特別處理。

### 3. 物理 hole rate 基線（用作未來異常偵測門檻）

機鑽 4/21-4/28 區間（排除 M03/M07 outliers）：

| 統計 | 值 |
|---|---|
| 範圍 | 0.85 – 1.85 孔 / RUN 秒 |
| 中位數 | ~ 1.20 孔 / 秒 |
| 最快 M06 | 1.85 |
| 最慢 M04 | 0.85 |

未來若某台 < 0.5 或 > 3，先懷疑 parser 或 counter rollover。

### 4. Hour-of-day 全廠模式（操作觀察）

- **12:00 午休清晰可見**：稼動率從 11:00 的 35% 跌到 12:00 的 22%，13:00 才回升 29%
- **6:00-7:00 接班空檔最低**：23% — 比午休還低
- **夜班 19:00-23:00 反而最高**（41-46%）— 比日班 8:00-15:00 的 33-37% 高 8-12 pp
  - 推測：夜班 setup 較少、批次跑得久；日班 changeover/break/intervention 較頻繁

可作為「日班生產力提升」討論的數據起點，但不一定是 bug。

### 5. 週末（Sat 4/25 + Sun 4/26）資料極稀

- 4/25 (Sat)：142 transitions / 7 active machines
- 4/26 (Sun)：3 transitions / 3 machines（極可能 idle 心跳）

本次 commit 加 toggle 控制，預設保留為 false（排除）以反映實際工作日 KPI。
未來若 ROI 評估需要包含週末，前端切換即可。

### 6. Commit `bc57b33` 訊息與 diff 不符

> "Enable Kataoka laser monitoring for L2-L4"

但實際 diff 只把 L2 設成 `enabled: true`，L1/L3/L4 仍 `false`（後者是 `b5b39a9`
才真正啟用）。`git log` 看起來像 4/1 就啟用 L2-L4，實際只 L2。

不修（git history immutable，amend 會打亂下游）。注意：未來若有人靠 commit
log 反推時程，要記得這條訊息誤導。

## 已確認 *正確* 的資料（給信心用）

- Takeuchi 機鑽 hole_count：DB sum vs raw counter delta，全 18 台誤差 ±2.5% 內，平均 < 1%
- state_transitions：零 self-loop、零同秒重複、跨日 carryover 邏輯運作正確
- M13 / 4-27 / hour 11 utilization：DB 84.1% == raw 3026 RUN 行 / 3600，秒級對齊

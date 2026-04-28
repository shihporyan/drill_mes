# 資料異常：2026-04-01 / 04-02 孔數爆量（百萬級）

## 觀察

UI 的稼動排行 → 4 月 W1 視圖（Mar 30–Apr 5）某些日有極端孔數：

- 4/1（三）：348,118,435 孔
- 4/2（四）：198,050,722 孔（推測；實際 cell 看到的是 198M 等級）

合理日孔數應在 ~10⁵–10⁶ 範圍（單台單日 ~200K–500K，全廠 ~10 台會跑就 ~5M），上述值約是合理值的 10⁰–10² 倍，幾乎肯定是 **backfill 重複計算**。

## 推測原因

- 跟 [project_peek_ahead_replay_bug](file:///Users/ryanhsu/.claude/projects/-Users-ryanhsu-Documents-drill-mes/memory/project_peek_ahead_replay_bug.md) 同類：Drive.Log 跨 batch peek-ahead 重複累計。
- 修復 commit 是 4/23 完成的，但歷史 `hourly_utilization.hole_count` 沒回補。
- 4/1–4/2 在 4/23 修復前累計，疑似是早期 buggy backfill 跑過。

## 影響範圍

- 影響 UI 顯示：稼動排行的「各日孔數」、稼動分析的「機台月孔數」。
- 不影響稼動率本身（utilization 由 run_seconds 計算，孔數是另一條欄位）。

## 處理建議

1. Query 找出 `hole_count > 50,000,000` 的 row（單台單小時不可能超過 50M）：
   ```sql
   SELECT machine_id, date, hour, hole_count
   FROM hourly_utilization
   WHERE hole_count > 50000000
   ORDER BY hole_count DESC;
   ```
2. 確認哪幾天受影響後，跑 `tools/dev_parse_backfill.py` 對應日期的 replay rebuild。
3. 或直接 SQL 把離群值歸零，等下次 hourly rebuild 自動補上正確值。

## 優先度

中。不影響稼動率（主要 KPI），但孔數會誤導報表。建議 4/29-4/30 處理。

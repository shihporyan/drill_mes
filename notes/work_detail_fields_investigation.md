# 作業細節欄位 — DB 覆蓋度待調查

**主旨：** Dashboard 需要顯示每台機器「正在做什麼」，目前 DB 存了哪些、缺了哪些、資料來源各是什麼，需要在初次 LOG 分析後逐項確認。

## 需要追蹤的欄位

| 欄位 | 目前 DB 狀態 | 資料來源 | 待確認事項 |
|------|-------------|---------|-----------|
| 工號（程式名稱） | `machine_current_state.work_order` — 從 col4 regex `O(\d+)\.(B\|T)` 萃取 | Drive.Log col4 | regex 只匹配 `.B`/`.T`，是否有其他後綴（`.A`/`.C` 等）？`O100.txt` 等非生產程式如何處理？ |
| 板號（A/B/C 板識別） | `machine_current_state.work_order_side` — 目前只存 B 或 T | Drive.Log col4 後綴 | `.B`/`.T` 到底是「Bottom/Top」還是「Board B / Board T」？實際 LOG 中有沒有出現 A/C/D 等？如果 Drive.Log 無法區分，是否需要讀 FILE.Log？ |
| 針型/針徑 | `machine_current_state.drill_dia` (col8)、`state_transitions.drill_dia` | Drive.Log col8 | 只存最新值和轉換時的值。是否需要歷史紀錄（例如每次換針的時間軸）？ |
| 進度/孔數 | `hourly_utilization.hole_count`（每小時差值）、`machine_current_state.counter`（最新累計值） | Drive.Log col10 | 孔數只有每小時彙總，沒有按工號分。要做「每片板鑽了多少孔」需要把 col10 差值和程式名變更事件交叉比對 |

## 已知事實

1. **Drive.Log col4（程式名）** 是唯一的工號來源。範例值：
   - `O2604007.T`、`O2604022.B` — 生產程式
   - `O100.txt` — 非生產程式（歸位/待機用），出現頻率很高
   - `G200` — 手動模式程式

2. **parser 的 regex** `^O(\d+)\.(B|T)$` 只匹配 B 和 T，忽略大小寫。如果實際有 `.A`/`.C` 等後綴，會被漏掉。

3. **工號目前只存在 `machine_current_state`**（最新一筆），沒有歷史紀錄。`state_transitions` 有 `program` 欄但沒有拆出 work_order。

4. **FILE.Log** 可能包含 NC 程式載入事件（何時載入哪個程式），但目前完全沒有被解析。

5. **TX1.Log** 包含操作者動作紀錄，可能有板號相關資訊，但也未被解析。

## 待辦（初次分析後執行）

- [ ] 用 `analyze_test_run.py` 跑完初次分析，確認 col4 實際出現的所有不重複值
- [ ] 確認 `.B`/`.T` 以外是否有其他後綴
- [ ] 檢查 FILE.Log 內容格式，評估是否值得解析
- [ ] 決定是否需要新增 `work_order_history` 表（紀錄每次工號切換的時間和孔數）
- [ ] 決定板號的正確含義和辨識方式

---
*建立日期：2026-04-13*

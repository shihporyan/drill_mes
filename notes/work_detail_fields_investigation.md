# 作業細節欄位 — 調查完成

**主旨：** Dashboard 需要顯示每台機器「正在做什麼」，目前 DB 存了哪些、缺了哪些、資料來源各是什麼。

## 需要追蹤的欄位

| 欄位 | 目前 DB 狀態 | 最佳資料來源 | 調查結論 |
|------|-------------|-------------|---------|
| 工號（程式名稱） | `machine_current_state.work_order` — 從 col4 regex 萃取 | **TX1.Log** `FILEOPERATION LOAD` 事件 | Drive.Log col4 幾乎都是 O100.txt（見下方說明），不可靠。TX1.Log 的 LOAD 事件是最佳來源 |
| 板號（T/B 面識別） | `machine_current_state.work_order_side` — B 或 T | TX1.Log / Drive.Log col4 後綴 | `.T` = Top（表面），`.B` = Bottom（裏面），無其他後綴 |
| 針型/針徑 | `machine_current_state.drill_dia` (col8)、`state_transitions.drill_dia` | Drive.Log col8 | 目前夠用，暫不需要歷史紀錄 |
| 進度/孔數 | `hourly_utilization.hole_count`、`machine_current_state.counter` | Drive.Log col10 | 按工號分孔數需搭配 TX1.Log 工號切換事件，暫不實作 |

## 調查結果

### 1. col4 不重複值

**4/10~4/12（analyze_test_run.py）：**
- M13: 3 種 — `O100.txt`(97.4%), `G200`(2.5%), `LASER-URA.T`(0.03%)
- M14: 1 種 — `O100.txt`(100%)

**4/3~4/9（dev_logs 完整掃描）：**
- M13: 17 種程式名，生產工號合計 <0.2%
- M14: 5 種程式名，生產工號合計 <0.1%
- 特殊：`GR2604003.T`（GR 前綴，5,136 行，只在 M13 4/7~4/8 出現）

### 2. 為何 col4 幾乎都是 O100.txt

**操作員工作流程：**
1. 載入生產工號程式（如 O2604025.T）— 提取鑽孔座標
2. 座標被存入 O100.txt
3. 機台以 O100.txt 為主程式開始鑽孔

因此 Drive.Log col4 在鑽孔期間幾乎永遠顯示 O100.txt，生產工號只在載入/提取的瞬間短暫出現。

### 3. 後綴分析

**Drive.Log col4 中只出現三種後綴：**
- `.T` — 1,023 行（Top/表面）
- `.B` — 22 行（Bottom/裏面）
- `.TXT` — 993,627 行（O100.txt 非生產程式）

**無 `.A`/`.C`/`.D`。**

FILE.Log Copy 事件常成對出現（O2604017.T → O2604017.B），與 PCB 雙面鑽孔一致。

TX1.Log 中有一筆 `O2502016.T1`（`.T1` 後綴），但從未出現在 Drive.Log col4。可能是特殊版本標記，暫不處理。

### 4. 各 LOG 的工號資訊比較

| LOG 類型 | 格式 | M13/M14 一致？ | 工號事件數/天 | 可靠度 |
|----------|------|---------------|-------------|--------|
| **Drive.Log col4** | CSV 欄位 | 一致 | 極少（<10） | 低 — 幾乎都是 O100.txt |
| **FILE.Log** | M13=Copy, M14=LoadProgram+全文 | **不一致** | M13:~5, M14:~8 | 中 — 格式不統一 |
| **TX1.Log** | `OpeLog : FILEOPERATION ... OPERATION:[LOAD] NAME:[程式名]` | **一致** | ~10-50 | **高 — 格式統一，兩台機器都有** |

**TX1.Log `FILEOPERATION LOAD` 事件格式：**
```
YYYY/MM/DD HH:MM:SS.mmm OpeLog : FILEOPERATION SCREEN:[PROGRAMLIST] OPERATION:[LOAD] NAME:[O2604025.T]
```

**工號推斷邏輯：** O100.txt LOAD 之前最後一個被 LOAD 的生產程式 = 當前正在加工的工號。

### 5. FILE.Log 格式（備參考）

**M13** — 檔案傳輸記錄（Shift-JIS）：
```
YYYY/MM/DD HH:MM:SS.mmm Copy ファイル読[source]指定[dest]
```
量極少（每天 ~1-10 筆），記錄從 USB/網路複製程式檔到機台。

**M14** — 程式載入 + NC 碼全文傾印：
```
YYYY/MM/DD HH:MM:SS.mmm LoadProgram "D:\Takeuchi\NcProgram\O2604031.B "
O100
M98P123
... (完整 NC 程式碼)
[EOF]
```
量很大（一天幾千到幾十萬行），包含程式碼全文。

### 6. work_order_history 表

**暫不新增。** 先把 TX1.Log 解析做好能追蹤工號切換事件，等需求更明確再設計專用表。

## 待辦

- [x] 用 `analyze_test_run.py` 跑完初次分析，確認 col4 實際出現的所有不重複值
- [x] 確認 `.B`/`.T` 以外是否有其他後綴
- [x] 檢查 FILE.Log 內容格式，評估是否值得解析
- [x] 決定是否需要新增 `work_order_history` 表 → 暫不需要
- [x] 決定板號的正確含義和辨識方式 → `.T`=Top, `.B`=Bottom
- [x] 擴展 WO_PATTERN 支援 GR 前綴（已在 drive_log_parser.py WO_PATTERN 加入 GR）
- [x] 修改 setup_dev_logs.py 複製 TX1.Log 和 FILE.Log（已完成，COMPANION_SUFFIXES）
- [x] （後續任務）建立 TX1.Log parser（parsers/tx1_log_parser.py，解析 FILEOPERATION LOAD 更新 work_order）

---
*建立日期：2026-04-13*
*調查完成：2026-04-13*

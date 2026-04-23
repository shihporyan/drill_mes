# Drill Monitoring System - Dev Notes

## Dev Environment (Mac)

開發在 Mac，測試環境在 Windows（連鑽孔機、無網路）。Mac 用 mock LOG 開發，Windows 只做最終驗收。

**切換方式：** 設定 `DRILL_DEV_CONFIG` 環境變數即可，不影響 production config。

```bash
# 啟動開發環境
DRILL_DEV_CONFIG=config/settings.dev.json python3 main.py --server-only
# http://127.0.0.1:8080

# 更新 LOG 後重新解析
python3 tools/setup_dev_logs.py
DRILL_DEV_CONFIG=config/settings.dev.json python3 tools/dev_parse_backfill.py
```

**Dev vs Production 差異：**
- `config/settings.dev.json`: backup_root=`dev_logs/`, db=`drill_monitor_dev.db`, host=`127.0.0.1`
- `config/settings.json`: backup_root=`C:\DrillLogs`, db=`drill_monitor.db`, host=office NIC IP

**新增 LOG 流程：**
1. 從 Windows 複製機台 LOG 資料夾到 `original_logs/machine_logs/`（命名格式：`M##-LOG-YYMMDD-TIMEHHMM`）
2. `python3 tools/setup_dev_logs.py` — 自動建立 parser 預期的目錄結構
3. `DRILL_DEV_CONFIG=config/settings.dev.json python3 tools/dev_parse_backfill.py` — 增量解析（只處理新資料）
4. 如需完全重建：刪除 `drill_monitor_dev.db` 後重跑 init_db + backfill

**gitignore 已排除：** `dev_logs/`, `drill_monitor_dev.db`, `drill_monitor_dev.log`

## E2E Tests (Playwright)

前端用 Playwright 測試，截圖統一輸出到 `screenshot/` 目錄。

```bash
# 首次安裝
npm install && npx playwright install chromium

# 跑測試（自動啟動 dev server）
npm run test:e2e

# 有瀏覽器畫面的模式（debug 用）
npm run test:e2e:headed

# 互動式 UI 模式
npm run test:e2e:ui
```

**前提：** dev DB 需有資料（跑過 backfill），否則 UI 會是空的。

**測試檔案：** `e2e/dashboard.spec.ts`（UI）、`e2e/api.spec.ts`（API contract）

**截圖輸出：** `screenshot/`（已 gitignore）

## Notes (工作筆記)

非程式碼的工作筆記、討論重點、領域知識備忘都放在 [notes/](notes/) 目錄下，每則一個 `.md` 檔。
用途：記錄與機台操作手討論的內容、LOG 格式觀察、待辦的流程改善等需要長期保留但不屬於 code 或 doc 的資訊。

目前筆記：
- [notes/laser_board_identification.md](notes/laser_board_identification.md) — 雷鑽 `.lsr` 宣告夾帶板名的可行性
- [notes/work_detail_fields_investigation.md](notes/work_detail_fields_investigation.md) — 作業細節欄位調查（已完成）
- [notes/work_detail_fields_plan.md](notes/work_detail_fields_plan.md) — 作業細節欄位規劃（機械鑽孔，討論中）
- [notes/m13_firmware_gap_investigation.md](notes/m13_firmware_gap_investigation.md) — M13 Drive.Log 時間跳躍/倒退現象
- [notes/tx1_flush_latency_investigation.md](notes/tx1_flush_latency_investigation.md) — TX1.Log flush 延遲調查計畫 + 4/20 兩次測試結果（state 轉態驅動 flush 已驗證）
- [notes/tx1_transition_triggered_parse_plan.md](notes/tx1_transition_triggered_parse_plan.md) — Layer 2 優化規劃：state 轉態觸發 TX1 parse，預期工單切換偵測壓到 <5 min

# MES × 鑽孔監控資料整合 — 規劃討論

**日期**: 2026-05-11
**背景**: 老闆會議提出，要把鑽孔監控的稼動率資料整合進 MES，回答以下問題：
- 稼動率低是因為「沒單做」？
- 還是因為點數少（前置作業占比高 — 找板、切板）？
- 還是「人員配置不良」？

鑽孔課長的補充：
- 即使點數少且機台有餘裕，正常會等到早上工單確認後才開始鑽點數少的單
- 點數少的瓶頸通常不在切削，而在「看 / 找板 / 切板材」
- 交叉比對時要小心**併版**（多張 WO 合進同一 O100.txt 鑽）

老闆要在資料整合後回答的具體問題：
- 稼動率、訂單點數、等待時間、工單加工時間 交叉比對
- 等待時間 / 工單加工時間 的正確率如何驗證（含併版情境）
- 鑽孔進出站時間可信度多高

---

## 1. 資安可行性 — 簡單，但要選對方案

DB 是單機 SQLite 檔。問題不是「能不能讓 MES 讀到」，是「哪種方式才會過資安稽核」。

### 三個方案比較

| 方案 | 即時性 | 資安門檻 | 評估 |
|---|---|---|---|
| **A. 定時 snapshot push** (CSV / .db copy 到 MES 共用區) | 10-30 min 或日級 | 最低 — 單向、唯讀、檔案層級 | **首推** |
| **B. Read-only HTTP API** (擴充現有 :8080 dashboard) | cycle 級 (~10 min) | 中 — 要打 firewall + IP whitelist + token | 適合近即時看板 |
| **C. MES 直連 SQLite 檔** (SMB share DB) | 即時 | 高 — 跨 SMB locking/WAL 風險，DB 暴露 | **不建議** |

### 必須先處理的前置 caveat
- 運算電腦目前綁 NIC2 (192.168.2.50)，沒 IP whitelist、沒 NSSM service、沒 TLS（見 `notes/dashboard_lan_rebind_20260508.md`）
- 運算電腦無 internet、無 NTP — 時間同步要靠 MES server 那端做基準
- prod 沒 sqlite3.exe — 工具都得用 Python stdlib 寫

### **決策 (2026-05-11)**: 採方案 A（每日 snapshot push）
- 不需要即時資料，每天抓一次足夠
- 可以手動 / 半自動推
- 資安門檻最低，且彙整計算放 MES 端，鑽孔端只負責 export

---

## 2. 對接前鑽孔端必須補的兩個資料缺口

### 缺口一：機鑽沒有 `work_orders` 表
- 雷鑽有 `laser_work_orders`（start / end / duration / hole_count 都齊）✅
- 機鑽只有 `machine_current_state`（即時 snapshot）+ `state_transitions`（機台層級）+ `o100_snapshots`（板別 routing）
- **MES 要問「WO-2604008 在 M16 跑了多久、鑽了幾孔」目前沒歷史 WO 紀錄表可查**

→ 需新建 `kataoka_work_orders` 表，從 state_transitions × o100_snapshots × machine_current_state 反算每張 WO 的 start / end / run_seconds / hole_count。**這是 MES 整合的最大 blocker。**

### 缺口二：已知資料品質問題未全清
- 9 台 Takeuchi TX1 timezone 設定錯（offset=1 沒設）— 5/9 已確認，machines.json 待修
- 5/8 假日 / weekend 是否生產的判定還沒定案
- 雷鑽 hole_count parser bug 5/9 才修（commit b246657），5/3 之前需做完整 backfill 稽核

---

## 3. 老闆三個問題的可解性分析

### Q1: 「稼動率低是因為沒單做嗎？」 → **可解**
需要 MES 提供「鑽孔站佇列在每個小時的 WO 數量」。鑽孔端提供「機台 STOP 時段」。交叉：

```
機台 STOP 且 MES 佇列 = 0 → 「沒單做」
機台 STOP 且 MES 佇列 > 0 → 「有單沒做」（人員 / 設備 / 併版等待）
```

難度：低。只要 MES 有站別 in/out 報工時間就行。

### Q2: 「是不是點數少所以前置作業占比高？」 → **可解，但需重新定義稼動率分母**

課長講的關鍵 — 點數少的 WO 瓶頸在「找板 / 切板 / 上料」不在切削。
現在 `hourly_utilization.utilization` 只看 RUN 秒數，對點數少 WO 一定難看。

要回答這題，時間軸要切成：

```
WO 在機台的總時間 = setup_time   (RESET/STOP 但 WO 已 load)
                 + run_time     (RUN 累加)
                 + idle_after   (RUN 結束到下一 WO load)
```

現只有 `run_time`。setup_time 要靠 `machine_current_state.work_order` 變化點推算 — 但這欄沒歷史（同缺口一）。

→ **不能算 setup 時間是個硬傷**。修缺口一即解。

### Q3: 「沒單 vs 人員配置不良？」 → **部分可解，需新訊號**

機台 LOG 看不出操作員在不在站邊。能做的逼近：
- MES 顯示鑽孔站有 WO，但機台 STOP > N 分鐘 → 標記「疑似人員問題」
- 無法區分「人在但在處理併版 setup」vs「人不在站」
- 要分乾淨需站邊感應或課長人工標記異常時段

老闆若要方向性指標 OK；要硬 KPI 還缺訊號。

---

## 4. 三個「正確率」問題

### 等待時間 = T1（WO 抵鑽孔站）- T2（機台首次 RUN）
- **T1 來自 MES**（上站完成 / 移轉報工）— 鑽孔端無法驗證
- **T2 鑽孔端可給**，但要先定義「首次 RUN」含不含 setup
- 正確率取決於 MES 報工的及時性，鑽孔端能做的是把 T2 算準

### 工單加工時間
| 場景 | 可信度 |
|---|---|
| 雷鑽 | 高（LSR 檔 ground truth） |
| 機鑽單一 WO | 中（RUN 區間連續性） |
| 機鑽併版 | **不可信** — 點數時間都無法乾淨分攤 |

### 進出站可信度

| 機種 | 進站訊號 | 出站訊號 | 可信度 |
|---|---|---|---|
| 雷鑽 | .lsr 檔出現 + 首 RUN | 末 RUN + LSR 完成 | ~95% |
| 機鑽（單 WO）| TX1 LoadProgram | `machine_current_state.work_order` 變更 | ~80%（受 TX1 flush 延遲、O100 孤兒編輯影響）|
| 機鑽（併版）| 同上，batch 級 | 同上，batch 級 | 個別 WO 不可歸屬，僅 batch 區間 |

---

## 5. 併版（concurrent boards）的處理 — 待老闆決策

操作員會合併多張 WD 進同一 O100.txt 省 setup（`notes/.../project_concurrent_boards.md`）。
`active_subs` 跨多個百位範圍時 = 併版（5/3 M16 觀察過 4 範圍）。

**核心問題**: 一個 batch 鑽完 3 張 WO 共 50,000 孔、40 分鐘 RUN，要怎麼把 50,000 孔和 40 分鐘拆給 3 張 WO？

| 方法 | 邏輯 | 優點 | 缺點 |
|---|---|---|---|
| A. 按 MES 預計點數比例分攤 | WO_A 預計 30k 孔 → 30k/總預計 × 實際 | 各 WO 都有數字 | 假設「預計：實際」同比例，實際不一定 |
| B. 按 sub-program 數量分攤 | WO_A 占 5 個 sub → 5/總 sub × 實際 | 純從 LOG 算 | 假設每 sub 點數相同，誤差大 |
| **C. 不拆分**，併版 batch 標記為合併紀錄 | 報表顯示「[WO-A, WO-B, WO-C] 合併批次，無法個別歸屬」 | 誠實 | MES 端無法給個別 WO 算稼動率 |

**建議 C** — 老闆問「點數少 WO 是不是 setup 久」時，併版本來就是為攤掉 setup，硬拆會混淆答案。
若老闆要 100% WO 覆蓋率報表，用 A 並標 confidence 旗標。

---

## 6. 分工 — 鑽孔端 vs MES 端

**決策 (2026-05-11)**: 計算放 MES 端做。原因：MES 主機在開發者旁邊，方便改動。

| 工作 | 負責端 |
|---|---|
| 機台狀態、WO start/end、hole_count、稼動率原始數據 | **鑽孔端** export |
| Setup time / 等待時間 / 加工時間切分計算 | **MES 端** |
| 併版識別與分攤邏輯 | **MES 端**（按上面選定方法）|
| 「沒單做 vs 有單沒做」交叉判斷 | **MES 端**（join MES 佇列資料）|
| 報表 / 看板 / 異常標記 | **MES 端** |

鑽孔端只負責提供乾淨資料，不做業務邏輯計算。

---

## 7. 執行順序

1. **跟老闆對齊 3 件事**（寫 code 前）
   - 「等待時間」「加工時間」「setup 時間」精確定義
   - 併版採 A / B / C 哪個處理方式
   - MES 那端能提供哪些欄位（佇列、抵站時間、預計點數）

2. **補鑽孔端缺口**（1-2 週）
   - 新建 `kataoka_work_orders` 表
   - 修 9 台 TX1 timezone
   - 跑雷鑽 hole_count v3 backfill 收乾資料

3. **MES 對接 — 方案 A（每日 snapshot push）**
   - 排程把 `kataoka_work_orders` + `laser_work_orders` + `hourly_utilization` 三張表 export 成 CSV
   - 定時推到 MES 共用網路位置（或手動觸發 + 排程兼用）
   - MES 端做 join 與計算，鑽孔端不碰 MES schema

4. **驗收期**：用 1 個月資料對 5-10 張 WO 做人工 ground truth 比對，確認交叉數字老闆敢拿來做決策。

---

## 8. 待決事項 (open questions)

- [ ] 老闆：等待時間 / 加工時間 / setup 時間的精確定義
- [ ] 老闆：併版採 A / B / C 哪個處理方式
- [ ] MES 端：能提供哪些欄位、什麼格式、什麼頻率
- [ ] 鑽孔端：`kataoka_work_orders` 表 schema 設計
- [ ] 鑽孔端：export CSV 欄位定義（含 confidence flag、是否併版 flag）
- [ ] 雙方：時間軸對齊基準（MES server 時間 vs 鑽孔運算電腦時間）

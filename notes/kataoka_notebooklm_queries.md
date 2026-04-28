# NotebookLM 查詢計畫 — Kataoka 雷鑽 LOG 與 panel 對應

要靠 NotebookLM（已上傳機器使用手冊）回答 3 個問題，每個問題分開查比較聚焦。

---

## 共通：先給 NotebookLM 的「背景描述」

把這段先丟進 chat 做共同前提（之後 3 個 prompt 都會接續）：

```
我們在開發機台稼動率監控系統，需要從 Kataoka 雷鑽機產生的 LOG 推算：
（A）目前正在哪個 station 加工
（B）每個 station 的起跑時間、累計加工時間
（C）目前在鑽的工單名稱與孔數

我們已知：
- 機台有 5 個 station（ST1~ST5），不是每台都全部用滿
- 操作員會在「加工配置」畫面把工單分配到不同 station，按下啟動後機台依序逐站鑽
- 同一時間只有 1 個 station 在鑽（排隊接力，非並行）
- LOG 檔放在 D:\LaserDrillingProcess\Log\，包含：
  - YYYYMMDD_ClsLaserCom.log
  - YYYYMMDD_ClsPLCTrd.log
  - YYYYMMDD_ClsExcutTrd.log
  - YYYYMMDD_ClsGalvanoTrd.log
  - YYYYMMDD_PhysicalMemory.log
  - YYYYMMDD_Frm*.log（Frm11200, Frm50000, Frm01000 等）
- 工單 / 排程資訊在 D:\LaserDrillingProcess\Info\：
  - YYYYMM_ProcTimeEnd.log（已完成工單）
  - ProcTimeStart.log（current batch）

請依序回答下列三個問題，每個問題都引用機器手冊的章節或頁碼。
```

---

## Prompt 1：ProcTimeStart 行為

```
問題 1：ProcTimeStart.log 的格式與覆寫時機

我們在現場抓到一份 ProcTimeStart.log，內容如下：

"3","2026/04/27 14:43:45","2026/04/28 05:36:22","53557.66"
"1","WD-2604093-TOP-G","C:\Users\KATAOKA\Desktop\WD-2604093\WD-2604093-TOP-G.LSR","S1000","3.000"
"3","GR-2604027-TOP-A","C:\Users\KATAOKA\Desktop\GR-2604027-內縮上A板6um\GR-2604027-TOP-A.LSR","SCM","1.500"
"4","WD-2604093-TOP-C","C:\Users\KATAOKA\Desktop\WD-2604093\WD-2604093-TOP-C.lsr","SCM","1.000"

操作員確認此次是「station 3 → station 1 → station 4」依序鑽，14:43:45 是整個 batch 的起跑時間。

請回答：
(a) 第一行開頭的「3」具體代表什麼？是 trigger station / 第一個排隊的 station / 當下在鑽的 station，哪一個？
(b) 第一行的 start_time 和 end_time 是「該 station 的時間」還是「整個 batch 的時間」？duration（53557 秒）是哪個範圍的合計？
(c) ProcTimeStart.log 何時會被覆寫？以下哪些事件會觸發覆寫？
    - 操作員按下 batch 啟動鈕
    - 某個 station 完成、自動切換到下個 station
    - 整個 batch 完成
    - 中途暫停 / 取消
(d) batch 進行中，當前面 station 完成換到下個 station 時，ProcTimeStart.log 是否會更新成新 station 的編號？

請引用手冊原文。
```

---

## Prompt 2：各 station 的 start_time / duration 在哪個 log

```
問題 2：找出「每個 station 個別的起跑時間 / 累計加工時間」存在哪個 LOG

操作員確認在機台 panel 的「進度」（進度按鈕點下去的視窗）可以看到每個 station 的：
- 加工開始時間
- 累計加工時間

但操作員不確定這些數字是從哪個 LOG 檔讀出來顯示的，也不確定是否有寫入磁碟。

請從手冊查：
(a) panel 上「進度」畫面顯示的 start_time / duration，對應的資料來源是哪個 LOG 檔？
    - 如果是 ProcTimeEnd / ProcTimeStart 以外的檔案，請列出檔名與欄位格式
(b) 如果這些資訊只存在記憶體沒寫入檔案，請明確說明
(c) ClsPLCTrd.log 裡面的「ProcSetWork」「ProcHeight」「GetRepeatTargetProcDetail」等事件，是否包含每個 station 的明確起訖時間戳？

提示：我們已觀察到 ClsPLCTrd 在 station 切換時會出現 ProcSetWork → ProcHeight → 本加工 序列，並包含「加工基盤番号:N」標明 station，所以可推算 start。但結束時間如何認定（特別是 batch 內的 station 切換點）需要手冊確認。

請引用手冊原文 + 對應的 LOG 範例行。
```

---

## Prompt 3：孔數 / 板別資訊 - 不依賴 LSR 檔

```
問題 3：孔數與板別在哪個 LOG 取得（不打開 LSR 檔的前提下）

我們無法存取機台桌面的 LSR 原始檔（權限限制），但需要：
(a) 每張工單的「孔數」（總共要打幾個孔）
(b) 每張工單的「板別」（A 板 / B 板 / TOP-A / BOT-C 等）

從手冊查：
(a) ClsPLCTrd 中「加工中POS番号」是孔位序號，是否能用最大值推出該工單的總孔數？例如最大 POS:1705 是否代表 1705 個孔？
(b) ClsExcutTrd.log 是否包含每張工單的孔數摘要？
(c) Frm 系列 LOG（Frm01000, Frm11200, Frm50000）的內容是什麼？是否與孔數 / 板別有關？
(d) 工單名稱中的 -TOP-A、-TOP-G、-BOT-C 是否是手冊定義的「板別」標準命名？操作員回報這個命名「有時會省略」，所以我們不能完全靠檔名判斷。是否有更可靠的板別欄位？

請引用手冊原文。
```

---

## 要附加給 NotebookLM 的範例 LOG（如果它支援額外檔案）

NotebookLM 可上傳「來源」（最多 50 個）。除了手冊本身，建議再上傳：

1. `ProcTimeStart.log`（373 bytes，前面的範例）
2. `202604_ProcTimeEnd.log`（20 KB，整月歷史）
3. `20260427_ClsLaserCom.log`（9 KB）
4. `20260427_ClsPLCTrd.log` 開頭 100 行 + 14:43:43 附近 50 行 + 17:57 附近 30 行（截取片段）
5. `20260427_ClsExcutTrd.log`（579 bytes）
6. `20260427_Frm50000.log`（635 bytes）
7. 任一筆 `*.lsr` 檔（如果取得到）
8. ClsPLCTrd 的「加工基盤番号」事件相關行（Grep 後另存）

範例 LOG 全部來自：`/Users/ryanhsu/Documents/drill_mes/original_logs/verify/drill_sample_L_20260427/programs/`

---

## NotebookLM 回答後要我更新的東西

拿到答案後，我會：
1. 修改 [parsers/laser_log_parser.py](../parsers/laser_log_parser.py) 對應的解析邏輯
2. 加入孔數 / 板別欄位到 [db/schema.sql](../db/schema.sql)（如有需要）
3. 更新 [notes/laser_board_identification.md](laser_board_identification.md)
4. 更新對應的 dashboard 顯示

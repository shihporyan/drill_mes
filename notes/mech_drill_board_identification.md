# 機鑽子板別 (A/B/C/D/E/F/G) 識別調查

**主旨：** 一張機鑽工單（如 O2604116.T）含多片板（A/B/C/D/E/F/G），需要知道機台「現在在鑽哪一片」。

> 雷鑽板別另案，見 [laser_board_identification.md](laser_board_identification.md)。

> ⚡ **5/3 重大突破 — 機制完全釐清，跳到下方「5/3 突破：O100.txt 機制 + 實作計畫」章節看最新理解。** 4/30 之前所有調查（INPUT 事件 / MACRO BLK / R 參數 / fingerprint）都不是主路徑，只是誤打誤撞接近真相的旁支證據。

## 5/3 突破：O100.txt 機制 + 實作計畫

### 突破來源

操作員親自示範並錄影逐字稿，加上提供 [M14_0503/](../original_logs/verify/M14_0503/) 與 [M13_0503/](../original_logs/verify/M13_0503/) 兩份完整 sample（含 NcProgram 目錄、O100.txt 本體、6 種 LOG）後，整個機制終於清楚。

### 真實的識別機制

```
┌──────────────────────────────────────────────────────────────────┐
│  操作員工作流程（每次換板）                                      │
├──────────────────────────────────────────────────────────────────┤
│  1. 機台停止（spindle off）                                       │
│  2. 操作員手動編輯 D:\Takeuchi\NcProgram\O100.txt                 │
│     — 把 O100 段下的 M98P### 改成下一片板要用的 sub 編號          │
│  3. 操作員在 TX1 螢幕按 [PROGRAMLIST] → LOAD O100.txt             │
│     → 機台 ReadFile 進記憶體                                      │
│     → TX1.Log 同步寫 3 個事件（見下節）                           │
│     → M14 機型同時把整個 O100.txt 內容 dump 進 FILE.Log           │
│  4. 操作員按 START，機台開始鑽（M98P### → 跳對應 sub 子程式）     │
└──────────────────────────────────────────────────────────────────┘
```

**關鍵：O100 段下的 `M98P###` 列表 = 當前正在鑽的板別所需的 sub-program 集合。** 對映 NC 表（如 WD-2604116 的 F=130,131,132）即可知板別。

### O100.txt 結構（實例：M14 5/3 抓回的 live 檔）

```nc
O100              ← entry section（自動執行模式：當前要鑽的板）
M98P127           ← call sub O127
M98P128           ← call sub O128
M98P102           ← call sub O102
M99               ← return

O200              ← 手動操作模式 sub（一次跑單針，與板別無關）
M98P100
M02

O300              ← 手動操作模式 sub（含 tool change + Z 設定）
T42
M98P127
G200Z2.585
M98P128
...
M02

O400 / O500 / O990  ← 其他輔助模式（測試 / 維護 / 校正）
```

→ **解析時只取 `O100` 段下的 M98P### 列表**。其他 section（O200/O300/O990）是操作員手動模式的 macro，跟「現在在鑽哪片板」無關。這也解釋了為什麼之前 4/30 看到 `INPUT:[O200/O300]` 都是 generic — 操作員在用手動模式 setup，不是切換板別。

### TX1.Log 訊號（每次 LOAD 同步 3 個事件）

```
2026/05/02 07:51:50.104 【ReadProgram】LoadProgram(D:\Takeuchi\NcProgram\O100.txt )
2026/05/02 07:51:50.159 【LoadProgram】ReadFile(D:\Takeuchi\NcProgram\O100.txt )
2026/05/02 07:51:50.159 OpeLog : FILEOPERATION SCREEN:[PROGRAMLIST] OPERATION:[LOAD] NAME:[O100.txt]
```

3 個事件時間幾乎同步（差 50ms 內），任一個都可當 trigger。我們現有 [tx1_log_parser.py](../parsers/tx1_log_parser.py) 已在解析第三條 `FILEOPERATION LOAD` — 可在同處加 hook。

**M13 5/2 觀察：當天 7 次 LoadProgram O100.txt 事件**（11:17 / 11:18 / 11:48 / 14:30 / 14:32 / 15:39 / 14:26 經 EXECSELECT）— 操作員一天會多次切板。

**M14 5/2 觀察：當天 9 次 LoadProgram O100.txt 事件**（07:51 / 07:54 / 08:51 / 09:39 / 13:03 / 14:20 / 15:32 / 16:00 / 16:01）。

**5/3 觀察：M13/M14 兩台都只有早上的 spindle on/off（自動測試），無 LoadProgram 事件 — 操作員當天還沒上工。**

### 兩種機台行為（重大差異）

| 機型 | FILE.Log 行為 | 取得 O100.txt 內容方式 | 適用 parser |
|------|--------------|------------------------|------------|
| **M14 系列** | **整個 O100.txt 內容直接 dump 進 FILE.Log**（含 `[EOF]` 標記）| 不需讀 live 檔 — 直接從 FILE.Log 解析 | M14 only |
| **M13 系列** | FILE.Log 只記 `Copy ファイル読[src]指定[dst]` 事件，不 dump 內容 | 必須在 TX1 LoadProgram 事件時讀 live SMB 檔 | M13 + 其他 |

**驗證：**
- M14 5/2 16:01:56 FILE.Log dump vs 5/3 早上抓的 live `NcProgram/O100.txt` → **內容一字不差**
- M14 5/2 16:00:04 vs 16:01:56 兩次 dump 內容**有微調**（`G200Z2.58` → `G200Z2.585`）— 證明每次 dump 都是當下真實內容，不是 cached
- → FILE.Log dump 是真實 source of truth

### 對映 NC 表 → 板別（最後一步）

NC 表 Excel（如 [`WD-2604116-...-(80X80).xlsx`](../original_logs/verify/WD-2604116-top4層40(3.5)(凸DAM+錫球)-bot-3層表單-(80X80).xlsx)）內 `top-nc` / `bot-nc` 兩個 sheet 列出每個 sub 編號對應哪片板：

```
top-nc:
A=O123,O124   B=O125,O126   C=O125,O126   D=O127
E=O128        E2=O129       F=O130,O131,O132   G=O130,O131

bot-nc:
A=O321,O322   B=O322   C=O322   D=O323   E=O324   F=O325   G=O325
```

例：M14 5/3 O100.txt 含 `M98P127, M98P128, M98P102`
→ 假設這是 WD-2604123（M14 NcProgram 內有 O2604123.T）
→ 需要 WD-2604123 的 NC 表 Excel 才能查 127/128/102 各對應哪板（現在沒有這份）

→ **NC 表必須由 MES（或工程端）提供**。但只要拿到對映表，從 LOG 端可以 **100% 識別當前板別**。

### 已驗證 / 待驗證

| 項目 | 狀態 | 說明 |
|------|------|------|
| O100.txt = 當前板的 sub-program 列表 | ✅ 已驗證 | M14 / M13 兩個 sample 一致 + 操作員親口確認 |
| TX1 LoadProgram O100.txt 事件 = 切板訊號 | ✅ 已驗證 | M14 5/2 9 次、M13 5/2 7 次，跟操作節奏一致 |
| O100 段以下 M98P### 是 sub 編號 | ✅ 已驗證 | 操作員指 `M98P130` 對應 NC 表 F=130 |
| M14 FILE.Log dump 內容 = 真實 live 檔 | ✅ 已驗證 | 5/2 最後 dump = 5/3 抓的 live 檔（一致） |
| 不同 LOAD 之間內容會變 | ✅ 已驗證 | M14 5/2 16:00 vs 16:01 有 Z 高度微調 |
| O100.txt 是覆寫式（單檔） | ✅ 已驗證 | NcProgram/ 只有一個 O100.txt |
| **SMB mtime 對 O100.txt 是否 lazy** | ⚠️ 未驗證 | M01/M03/M17 已知 lazy（[project_smb_lazy_mtime.md](../project_smb_lazy_mtime.md)），但只測過 .Log 系列。O100.txt 通常 <1KB，可能行為不同 |
| **18 台機台「dump 全文 vs 不 dump」分類** | ⚠️ 待調查 | 已知 M14 dump、M13 不 dump；其他 16 台未確認 |
| **NC 表 Excel 命名規則 + 取得通道** | ⚠️ 未確認 | MES 整合前需手動取，要跟工程端確認檔案儲存位置 |

## 實作計畫（5/3 起）

### Phase 1：O100.txt parser ✅ 開工中

**目標：** 純解析模組，吃 O100.txt 內容回傳結構化結果。

**檔案：** [parsers/o100_parser.py](../parsers/o100_parser.py)（新增）

**API：**
```python
parse_o100_content(text: str) -> {
    "active_subs": [127, 128, 102],   # O100 entry section 內的 M98P 編號
    "sections": {                      # 全部 section 解析（debug 用）
        "O100": [127, 128, 102],
        "O200": [100],
        "O300": [127, 128, 102, 101, 110, 103, ...],
        ...
    },
    "raw_lines": int,
}
```

### Phase 2：dev 抽取工具 ✅ 完成

**目標：** 兩種來源（M14 FILE.Log dump / M13 live file）都能跑出時間軸，互相驗證。

**檔案：** [tools/dev_extract_o100.py](../tools/dev_extract_o100.py)（新增）

**功能：**
- Mode A（M14 FILE.Log）：掃 FILE.Log → 取出每段 LoadProgram dump → parse → 印時間軸
- Mode B（M13 / 通用 TX1）：掃 TX1.Log 取 LoadProgram 事件 → 對 current snapshot 解析 → 印時間軸（dev 環境只能拿到一筆 snapshot，production 才能多筆）
- Cross-check：M14 模式跑完後比對「最後一次 dump hash」vs「live snapshot hash」，驗證 dump 是真實內容
- 輸出：每筆 `(machine, timestamp, source, active_subs, content_hash)`，方便比對

### Phase 1+2 實測結果（5/3，M14 + M13 sample）

#### M14 Mode A — FILE.Log 9 次 LoadProgram dump 全部解析成功

```
2026/05/02 07:51:50.159  active_subs=[127, 128, 102]   hash=3c7a20149b4e
2026/05/02 07:54:04.621  active_subs=[127, 128, 102]   hash=41f01b55b999  *CHANGE*
2026/05/02 08:51:46.833  active_subs=[127, 128, 102]   hash=5fe5c51eb203  *CHANGE*
2026/05/02 09:39:06.248  active_subs=[127, 128, 102]   hash=5692eb34006c  *CHANGE*
2026/05/02 13:03:07.830  active_subs=(none)            hash=75ae855fe76e  *CHANGE*  ← 異常
2026/05/02 14:20:08.148  active_subs=[127, 128, 102]   hash=6baa8c719579  *CHANGE*
2026/05/02 15:32:21.165  active_subs=[127, 128, 102]   hash=947942b2eb7c  *CHANGE*
2026/05/02 16:00:04.478  active_subs=[127, 128, 102]   hash=947942b2eb7c           ← 重按 LOAD
2026/05/02 16:01:56.135  active_subs=[127, 128, 102]   hash=e18115ff0191  *CHANGE*
```

**觀察：**
- M14 5/2 整天 active_subs 都是 `[127, 128, 102]` → 操作員沒換板，只調整其他 section（如 O300 內的 Z 高度）
- 13:03 那次 dump 內容含 `O100\nM98P\nM99` —— 操作員把 P 後的數字暫時清空（編輯到一半 LOAD 了）。parser 正確回傳 `[]`，**production 邏輯應視為 "in-progress edit" 不寫板別變更**
- 16:00 → 16:00 hash 完全相同 → 操作員可能不小心按了兩次 LOAD（dedup 邏輯可省 storage）
- 16:01 之後到 5/3 中午抓 sample 之前，又有變更（live snapshot 是 16:01 那次的內容）

#### M14 Mode B — TX1 9 次 LoadProgram 事件，全部 timestamp 比 Mode A 早 ~50ms

TX1 事件時間（07:51:50.104）比 FILE.Log dump 時間（07:51:50.159）早 55ms — 合理，因為 TX1 記 LoadProgram 開始，FILE.Log 記 dump 寫完。**Mode A 和 Mode B 事件數一致（9 = 9）**，可互相 sanity check。

#### M14 Cross-check ✅ MATCH

```
last dump @ 2026/05/02 16:01:56  hash=e18115ff0191
live snapshot                     hash=e18115ff0191
✅ MATCH  active_subs match: True
```

→ 證明 M14 FILE.Log 的 dump 內容跟 live SMB 檔**完全一致**，可信賴 FILE.Log 為唯一資料源（不需重複讀 SMB）。

#### M13 Mode B — TX1 6 次 LoadProgram 事件 + live snapshot

```
snapshot parsed: active_subs=[128, 102]  hash=bdfe9b073c6f
2026/05/02 11:17:36.792  active_subs=[128, 102]
2026/05/02 11:18:33.169  active_subs=[128, 102]
2026/05/02 11:48:47.849  active_subs=[128, 102]
2026/05/02 14:30:42.362  active_subs=[128, 102]
2026/05/02 14:32:20.391  active_subs=[128, 102]
2026/05/02 15:39:14.341  active_subs=[128, 102]
```

dev 環境只有一份 snapshot，所有事件都對應同一 hash。production 要在每次事件 trigger 時讀活檔才能拿到當下內容。

### Parser 邊界情況

整理出實作 Phase 3 寫入 DB 時要處理的邊界：

| 情境 | dump 內容 | parser 結果 | 建議邏輯 |
|------|----------|-------------|---------|
| 正常 | `O100\nM98P127\nM98P128\nM98P102\nM99` | `active_subs=[127,128,102]` | 寫入 DB |
| 操作員編輯一半就 LOAD | `O100\nM98P\nM99` | `active_subs=[]` | 不寫板別變更，可能寫一筆「進入編輯狀態」 |
| 重按 LOAD（hash 完全相同） | 同前次 | 同前次 | content_hash dedup → 不寫重複 |
| O100 段不存在 | （不太可能） | `active_subs=[]` | 同上邊界 |
| 含 L## 重複次數（如 M98P127L2） | 已處理 | 仍正確抓 127 | 直接抓數字部分 |

### 已驗證的解碼/編碼處理

- O100.txt: ASCII text + CRLF line endings → 用 `cp932` 解碼（向下相容 ASCII）+ `splitlines()` 自動處理 CRLF
- FILE.Log: CP932（含日文 prose）+ CRLF → 同上
- Hash normalization: `line.rstrip()` per line + `.rstrip()` 全文 → 排除 trailing 空行 / CRLF/LF 差異

### Phase 3：DB schema + 寫入（待用戶確認 schema 後做）

**草案：**
```sql
CREATE TABLE o100_snapshots (
    id INTEGER PRIMARY KEY,
    machine_id TEXT NOT NULL,
    captured_at TIMESTAMP NOT NULL,
    source TEXT NOT NULL,                  -- 'file_log' | 'live_smb'
    content_hash TEXT NOT NULL,            -- 內容去重用
    active_subs TEXT NOT NULL,             -- JSON: [127, 128, 102]
    raw_content BLOB,                      -- 完整 O100.txt（小檔，<2KB），保留供事後查
    UNIQUE(machine_id, captured_at)
);
CREATE INDEX idx_o100_machine_time ON o100_snapshots(machine_id, captured_at DESC);

-- 加到 machine_current_state（dashboard 即時欄位）
ALTER TABLE machine_current_state ADD COLUMN current_o100_subs TEXT;  -- JSON
ALTER TABLE machine_current_state ADD COLUMN o100_captured_at TIMESTAMP;
```

### Phase 4 prep：NcProgram SMB 連線可行性 recon ✅（5/3 設定變更後新增）

**背景：** 5/3 用戶開放 M13-M18 的 NcProgram 共享讀取權限。其他機台（M01-M12）暫未開放。先做 one-shot recon 確認哪幾台真的設好。

**檔案：** [tools/probe_o100_ncprogram_access.py](../tools/probe_o100_ncprogram_access.py)（新增）

**為什麼先做這個（在 SMB latency probe 之前）：**
- latency probe 預設 30s 一次 polling，若有機台路徑沒設好會持續刷錯誤
- one-shot recon 每台只 1× listdir + 1× stat + 1× 讀 ~500B → 對 production 影響近乎零
- 結果直接告訴我們：哪幾台 OK 可進 latency probe、哪幾台要回去找廠商重設

**部署指令：**
```cmd
cd C:\DrillMonitor
python tools\probe_o100_ncprogram_access.py
```

**輸出每台分為三類：**
- `OK` — 路徑通 + O100.txt 可讀 + 解析出 active_subs
- `DIR OK / O100 problem` — 目錄可列但檔案讀不到（perms 細節有差，需查）
- `BLOCKED` — 連目錄都列不到（share 設定沒生效）

**SMB 路徑（5/3 驗證）：** `\\{ip}\NcProgram` — NcProgram 是獨立 share，**不在** LOG share 下面。預設已更新。

**不需暫停 LOG collector：** probe 走 NcProgram share，LOG collector robocopy 走 LOG share，兩個獨立 share，不衝突。

**5/3 實測結果 — M13-M18 全部 6 台 OK：**

| 機台 | mtime | size | active_subs | 觀察 |
|------|-------|------|-------------|------|
| M13 | 2026-05-03 13:09 | 390B | `[128, 102]` | TOP only |
| M14 | 2026-05-02 15:01 | 523B | `[127, 128, 102]` | 跟我們本機 sample 完全一致 ✅ |
| M15 | 2026-05-02 18:06 | 395B | `[127, 102]` | TOP only |
| **M16** | 2026-05-03 13:18 | 1023B | **`[123, 124, 102, 223, 202, 322, 323, 302, 423, 402]`** | **10 個 sub，跨 1xx/2xx/3xx/4xx 範圍** — 可能是複雜多板 batch，待操作員確認 |
| M17 | 2026-05-03 07:44 | 447B | `[126, 102]` | TOP only |
| M18 | 2026-05-03 09:40 | 478B | `[121, 102, 324, 302]` | TOP + BOT 混合 — 可能在做 .T+.B 連續鑽 |

**M01-M12 perms 尚未開放** — 跟用戶討論是否一併開，或先靠 M14-style FILE.Log dump 處理。

### 併板（5/3 重大背景補充）

#### 什麼是併板

**「併板」= 把不同工單的板子放在**同一個機台檯面上**鑽孔**。操作員會把多張工單的 NC 子程式（含座標）合併編進**同一份 O100.txt**。

**為什麼這樣做：** 省換料/setup 時間。某些工單的孔數很少（小型板、低密度），如果單獨上機要 setup → 鑽幾分鐘 → 換料 → setup 下一單，效率太差。把幾張小工單併在一檯，可以一次 setup 跑完。

#### 對識別邏輯的衝擊

之前的假設：
> O100.txt 內 O100 段下的 M98P### 列表 = 當前**單一**板別

**併板實際情況：**
> O100.txt 內 O100 段下的 M98P### 列表 = 當前所有正在交錯鑽的板別（**可能跨多張工單**）

「機台現在在鑽哪片板」這個問題在併板情境下變成「機台現在在鑽**哪幾片板**」，是 1:N 關係。

#### M16 + M18 實證重新解讀（5/3 照片佐證）

操作員當下拍了 5 張 NC DATA 管理表照片（4 張 TOP，1 張 BOT，至少 2 張不同工單）。從照片推測編號規則 hypothesis：

| WD 序號 | TOP 範圍 | BOT 範圍 |
|---------|---------|---------|
| 第 1 張 | **1xx**（如 101~134） | **3xx**（如 301~329） |
| 第 2 張（併板時） | **2xx** | **4xx** |
| 第 3 張（併板時） | 5xx? | 7xx? |

→ 這個編號規則應該是 Takeuchi 機台或工程端定的 convention，避免併板時 M98P 編號衝突。**待跟工程端 / Takeuchi 廠商確認**。

**M16 = 確認併板：** active_subs `[123, 124, 102, 223, 202, 322, 323, 302, 423, 402]`
- `102, 123, 124` → WD #1 TOP
- `302, 322, 323` → WD #1 BOT
- `202, 223` → WD #2 TOP
- `402, 423` → WD #2 BOT
- → **併板 2 張工單，TOP+BOT 都做**（非常複雜的 batch）

**M18 = 不是併板，是單工單 .T+.B：** active_subs `[121, 102, 324, 302]`
- `102, 121` → 同一工單 TOP
- `302, 324` → 同一工單 BOT
- 都在 1xx/3xx 範圍 → **同一張 WD，操作員把 .T 和 .B 都編進 O100.txt 連續鑽**（板翻面）

**M13/M14/M15/M17 = 單工單單面：** active_subs 只有 1xx 範圍 → 純 .T 鑽孔中

#### 對 Phase 5（NC 表整合）的衝擊

之前計畫：
> Parser 自動找對應 WD 的 Excel → 內建「sub → 板別」對映 → 直接顯示「現在在鑽 F 板」

**併板情境的修正：**
- 一份 O100.txt **可能對應多張 WD** → parser 要先按編號範圍（百位數）切群組
- 每群組找對應的 NC 表 Excel → 才能查板別
- Dashboard 要顯示「**現在在鑽 WD-X 的 F 板 + WD-Y 的 B 板**」（多張 WD 同時鑽）

**對映規則的開放問題：**
1. 「百位數 → WD 序號」是否真的是固定 convention？需查證
2. 哪一張 WD 是「主工單」（用來查當前載入的 .T/.B 檔名 vs 併入工單）？
3. NC 表 Excel 命名規則 / 儲存位置 — 工程端怎麼歸檔？parser 要怎麼自動找到對應檔？
4. 如果 WD #2 是臨時併入的，工程端是否有專門的「併板清單」記錄哪些工單併在一起？

#### 照片證據（操作員 5/3 提供）

5 張 NC DATA 管理表照片，存在用戶端（暫未複製到本地）。觀察重點：
- 至少 2 張不同工單的 NC 表（料號 `089IF8` 和 `10IAM6002`，均為「欣興」客戶）
- TOP NC 範圍清楚標到 101-134
- BOT NC 範圍清楚標到 301-329
- 各板別欄位（A/B/C 等）以「○」記號標示哪個 sub 用於哪片板（如 `102 = ○ all` 代表 102 是 A/B/C 共用）
- 部分照片有 `板厚 1.6 / 0.8 / 1.0` 等差異 → A/B/C 板可能是不同板厚或不同板材的同類型板

### Phase 4：SMB 延遲驗證 ✅ probe 完成（待 ncprogram recon 確認後部署）

**檔案：** [tools/probe_o100_smb_latency.py](../tools/probe_o100_smb_latency.py)（新增）

**運作：** 長時間 background 跑（建議 1 整工作天 8h+），每 30s 對 18 台 Takeuchi 做：
1. Stat live SMB `\\{ip}\LOG\Takeuchi\NcProgram\O100.txt` → 記 mtime / size / hash
2. Scan 該機台今天 TX1.Log 找新 LoadProgram O100.txt 事件 → 記 tx1_event_ts
3. CSV 輸出 `tools/probe_results/o100_smb_latency_{start_ts}.csv`，欄位含 `latency_secs = smb_mtime - tx1_event_ts`

**部署指令（production Windows）：**
```cmd
cd C:\DrillMonitor
python tools\probe_o100_smb_latency.py
```

**判讀：**
- `latency_secs ≈ 0` 或負（TX1 領先<5s）→ SMB mtime 即時，可信賴 → M13-style 直接讀 SMB OK
- `latency_secs > 60s` → SMB lazy mtime 嚴重 → 必須加 hash polling fallback（在 parser 觸發 SMB read 時，先 stat 看 mtime；若還沒更新到事件之後，再等 N 秒重讀，直到 hash 變）

**SMB 路徑可調：** 用 `--smb-template` 改路徑（預設 `\\{ip}\LOG\Takeuchi\NcProgram\O100.txt`），如果實際 share 結構不同需校正。

### Phase 3 prep：18 機台 FILE.Log 行為分類 ✅ probe 完成（待部署）

**檔案：** [tools/probe_o100_classify.py](../tools/probe_o100_classify.py)（新增）

**運作：** 一次性掃描，讀本機 backup_root（已 robocopy 同步的 FILE.Log），每台機台檢查：
- FILE.Log 大小
- `[EOF]` 標記出現次數（M14 dump 簽名）
- `LoadProgram...O100.txt` 字串出現次數
- 分類為 `dump_style` / `tx1_only_style` / `inconclusive` / `no_data`

**部署指令（production Windows）：**
```cmd
cd C:\DrillMonitor
python tools\probe_o100_classify.py --days 7
```

**dev sample 已驗證邏輯正確：**
| Sample | size | EOFs | LOADs | 分類 |
|--------|------|------|-------|------|
| M14 5/2 | 1,119,158 B | 11 | 9 | **dump_style** ✅ |
| M13 5/2 | 149 B | 0 | 0 | **tx1_only_style** ✅ |
| M13 4/29~4/30 | <300 B | 0 | 0 | **tx1_only_style** ✅ |

**為什麼這個分類重要：** Phase 3 的 parser hook 邏輯**取決於**每台機台的 mode。寫死任一條都會錯一半機台。建議跑完這個 probe 拿到 18 台分類後，再設計 Phase 3。

### Phase 5（最終）：SOP / NC 表整合

- **NC 表來源：** 跟工程端要 NC 表的儲存路徑與命名規則 → parser 自動找對應 WD 的 Excel → 內建「sub → 板別」對映 → 直接顯示「現在在鑽 F 板」
- **MES 整合（長期）：** MES 直接給「當前工單 + 當前板」即不需 LOG 反推，但 LOG 反推可作為交叉驗證

---

## 以下為 4/30 之前的調查紀錄（保留為歷史脈絡）

> 4/30 之前所有調查（TX1 INPUT 事件 / MACRO BLK / R 參數 / drill bit fingerprint）都不是主路徑。但這些觀察讓我們最終理解「O100.txt 才是真正的 routing 檔」。保留下方紀錄以便未來如果其他 LOG 提供新線索時能快速比對。

---

## 修訂結論（4/30 晚間 — 推翻前一版）

**TX1.Log 的 `SCREEN:[INPUT] INPUT:[O###]` 事件就是板別識別線索。** 操作員在按下 `[E License Input]` 鈕後輸入的 O 編號 = NC 表的 sub-program 編號 = 對應板別。

證據：
1. **歷史照片**（[doc/IMG_3870.JPG](../doc/IMG_3870.JPG), IMG_3871, IMG_3872, IMG_3874）拍到 2026/04/02 17:09:08 在 M(?) 機台 LOAD `O2604006.B` 後，操作員 typing `INPUT:[O301]` — 對應 NC 表 `BOT-S26085A(2W)-NC DATA管理表-ABC.xlsx` 的 A/B/C 板
2. **本次 sample（4/30）M03 / M12 / M18 三台機**確實出現具體 sub-program 編號：
   - M03 08:18-08:19：`INPUT:[O301]`、`INPUT:[O201]`、`INPUT:[O401]`
   - M12 16:54：`INPUT:[O301]`
   - M18 11:44：`INPUT:[O328]`

## 為什麼 M13 4/30 沒看到？

**操作員行為不一致**：M13 4/29~4/30 兩天 TX1 全部 INPUT 只有 4 種 generic 值：
```
30× O200    3× O300    3× O990    1× O106
```

完全沒有 O123/O130 等 NC 表特定編號。但 IMG_3870 和 M03/M12/M18 證明系統支援這個 pattern — 是操作員在 M13 期間沒有輸入特定編號（可能用其他方式呼叫 sub，或當時不需要切換板別）。

→ **這個 ground truth 必須跨機台、跨工單收集才能確認可靠度。**

## 待辦：歷史結論需重新驗證

⚠️ **以前確認過此可能性（IMG_3870 系列照片是當時的記錄），但沒留下文字證據**。本筆記補上。仍需要更詳細的二次調查：

- [ ] 跨機台統計：哪些機台/操作員會輸入特定 sub 編號（O201/O301/O###）？哪些只用 generic（O200/O300）？
- [ ] 取得 NC 表 → INPUT O 編號 → 板別的對映規則（從 MES Excel：例如 `301 → A板`、`328 → ?`）
- [ ] 收集多筆「操作員口頭確認 + 實際 INPUT 事件」配對作 ground truth
- [ ] 確認 INPUT 事件是「進入新板鑽孔」前的必要步驟，還是可選的（=覆蓋率有多高）
- [ ] 詢問 Takeuchi 廠商：是否有更可靠的「sub-program 進入」事件 hook
- [ ] M14 FILE.Log 全文 dump 是否能反推 sub-program 結構（4/30 sample 沒抓到完整 dump）

## 關鍵 LOG 位置

```
2026/04/02 17:08:55  FILEOPERATION SCREEN:[PROGRAMLIST] OPERATION:[LOAD] NAME:[O2604006.B]
                       ↑ 工單 + 板面（.T/.B）
2026/04/02 17:09:04  FILEOPERATION SCREEN:[PROGRAMLIST] OPERATION:[LOAD] NAME:[O100.txt]
                       ↑ 機台主程式模板
2026/04/02 17:09:08  BUTTON PUSH SCREEN:[AUTO] BUTTON:[E License Input]
2026/04/02 17:09:08  SCREEN:[INPUT] INPUT:[O301]    ← 板別識別
2026/04/02 17:09:08  MESSAGE: "Under O number search."
```

## 觀察到的 INPUT O 編號分類（待驗證）

從 4/30 全 18 機台 sample 整理：

| 類型 | 範圍 | 出現機台 | 可能含義 |
|------|------|----------|----------|
| Generic | O100, O106, O200, O300, O990 | 幾乎全部機台 | O100.txt 內預設 macro section（不識別板別） |
| **特定 sub** | **O201, O301, O328, O401** | **M03, M12, M18** | **NC 表 sub-program 編號（識別板別）** ⭐ |
| O10 | O10 | M03 | 短編號，用途待查 |

**初步假設**（需驗證）：
- 開頭 1XX = TOP 系列 sub
- 開頭 2XX/3XX = BOT 系列 sub
- 開頭 4XX = 特殊系列
- 第 2-3 位數字 = 板別索引（A=1, B=2, C=3...?）

但 NC 表 WD-2604116 的編號是 `Top: 123-132 / Bot: 321-325`，看起來規則不只是「百位 = TOP/BOT」。需要更多 NC 表樣本才能確認規則。

## 4/30 晚間二次驗證：M13 全歷史 TX1 INPUT 比對 NC 表

**目的：** 用 WD-2604116 完整 NC 表編號，跨 M13 全部可用 TX1.Log 搜尋，驗證「INPUT [O###] = 板別」假設的覆蓋率。

### 從 Excel 解析的完整 NC 表編號範圍

直接讀 `WD-2604116-...-(80X80).xlsx` 取得：
- **top-nc sheet**：column A 有 O101 ~ O132（32 個 sub-program slot）
- **bot-nc sheet**：column A 有 O301 ~ O325（25 個 sub-program slot）
- 用戶口述的 A=123,124 / F=130,131,132 等是「實際分配給 A~G 板的子集」，不是全部範圍

### 搜尋範圍（M13 全部可用 TX1）

19 個 LOG 檔，跨 4/4 ~ 4/30：
- `dev_logs/M13/2026{0404,0405,0406,0407,0410,0411,0412,0413,0415,0416,0417}/...TX1.Log`
- `original_logs/verify/drill_sample_2026042{8,9}_*/M13/{27,28,29,30}TX1.Log`
- `original_logs/verify/0430/{29,30}TX1.Log`

### 結果 1：WD-2604116 在 M13 上只跑過 4/30 一天

`grep -c O2604116` 全 19 檔 → 只 4/30 sample 各 7 行 hit；其他歷史 LOG 都沒這個工單。

### 結果 2：對 WD-2604116 的 NC 表 O 編號 — 4/30 M13 完全沒有

| 範圍 | 4/30 M13 hits | 結論 |
|------|---------------|------|
| TOP O123-O132（NC 表 A~G 用） | **0** | F=130,131,132 完全不在 |
| BOT O301-O325 | 0 | 4/30 跑 .T，本來就不會有 BOT subs |

操作員口述「17:00~17:30 開始 F 板」期間實際 INPUT：
```
17:00:02  INPUT [O200]   ← generic
17:08:04  INPUT [O200]   ← generic
17:24:35  INPUT [O300]   ← generic
```

### 結果 3：M13 歷史上 **確實會** 輸入 NC-表類型編號（不同工單）

跨 4/4 ~ 4/16 dev_logs，找到 M13 操作員實際 typing NC-table-range 的編號：

| 日期 | 時間 | INPUT 值 | 範圍 |
|------|------|----------|------|
| 4/4 | 11:12 | **O121** | top-nc 範圍 |
| 4/4 | 15:35 | **O301** | bot-nc 範圍 |
| 4/5 | 16:11 / 18:48 / 19:39 | **O102** ×3 | top-nc 範圍 |
| 4/6 | 13:56 | **O102** | top-nc 範圍 |
| 4/7 | 08:45 | **O301** | bot-nc 範圍 |
| 4/7 | 11:01 | **O101** | top-nc 範圍 |
| 4/16 | 08:34 | **O101** | top-nc 範圍 |
| 4/30 | 16:26 | **O106** | top-nc 範圍（但發生在 LOAD WD-2604116 **之前**，屬於前一張工單）|

→ 證明 M13 操作員會用這個輸入方式，但**頻率很低**。

### 結果 4：M13 全歷史 INPUT 統計（覆蓋率）

| 類型 | 編號 | 次數 | 比例 |
|------|------|------|------|
| Generic（不識別板別） | O200, O300, O990, O100 | 271 | **86.6%** |
| **NC-table 特定** | O101, O102, O106, O121, O301 | **13** | **4.2%** |
| 短編號 / 雜項 | O5, O6, O02, O31, O901 | 19 | 6.1% |
| 其他 | — | ~10 | 3.2% |
| **合計** | | ~313 | 100% |

**M13 操作員只有 4.2% 機率輸入 NC 表特定編號** — 96% 用 generic 或短編號 → 從 LOG 反推板別**極不可靠**（除非改 SOP）。

### 對 WD-2604116 + F板的明確答覆

> 「TX1 內有沒有 130/131/132？」 → **沒有。**

但這不代表系統壞，而是 **M13 該操作員當時沒有輸入這些編號**，用 generic O200/O300 帶過。其他機台（M03/M12/M18）+ 歷史 IMG_3870 證明這個 input pattern 是存在的。

## 修訂的覆蓋率結論

| 想識別板別的方式 | 覆蓋率估計 | 說明 |
|------------------|------------|------|
| 從 TX1 INPUT [O specific] 反推 | **<5%**（M13 base） | 操作員行為不一致，多數時間用 generic |
| MES 過帳 + 工單派工資料 | ~100% | 終極解，等 MES 整合 |
| **改 SOP 強制輸入** | **可推到 >90%** | 主管/操作員端的決策，技術端配合 parser |

## 待辦補充（4/30 晚間二次驗證後）

- [ ] **跟主管/操作員談 SOP 變更可行性** — 每次換板必按 [E License Input] + 輸入 NC 編號
- [ ] 用 M03/M12/M18 4/30 樣本反推：那些操作員的 INPUT 跟換板事件時間是否強相關
- [ ] 統計全 18 機台「有 specific INPUT 事件」的比例 vs「只用 generic」的比例
- [ ] 針對 M03 (4/30 08:18 連續 O301/O201/O401) 找操作員確認當時是不是真的在切換 A/B/C 板

## 4/30 第一次實證細節（M13 + WD-2604116，反例）

**已知條件：**
- 工單：`O2604116.T`（top 4 層、bot 3 層、80×80）
- NC 表（從 MES Excel `WD-2604116-...-(80X80).xlsx`）：

  | Block | A | B | C | D | E | E2 | F | G |
  |-------|---|---|---|---|---|----|----|---|
  | Top-NC | O123,O124 | O125,O126 | O125,O126 | O127 | O128 | O129 | **O130,O131,O132** | O130,O131 |
  | Bot-NC | O321,O322 | O322 | O322 | O323 | O324 | — | O325 | O325 |

- 操作員口頭確認：M13 約 17:00~17:30 開始鑽 F 板 → 對應 Top-NC F = `130, 131, 132`

**結果：M13 全 6 類 LOG 找不到 130/131/132**（細節保留如下）

| LOG | 17:00+ 出現的數字 | 含 130/131/132？ |
|-----|-------------------|------------------|
| TX1.Log INPUT | 只有 O200/O300 generic | ❌ |
| Drive.Log col5 (program) | O100.txt, O2604116.T | ❌ |
| Drive.Log col8 (drill_dia) | 17:00+: 000/043/086 | ❌ |
| MACRO.Log R parameter | R(43)/R(84)/R(86) | ❌ |
| MACRO.Log BLK(1) 重置 | 共 10 次，R 都在 43/84/86 | ❌ |
| TARN.Log PC-ADD/MP-ADD | 範圍 0-24 | ❌ |
| Alarm.Log | 5821/5820/1322/6802/5537 | ❌ |
| FILE.Log | 只有 O2604116.T 主檔 Copy | ❌ |

**所有看似 hit 的 130/131/132 全是時間戳毫秒（`.130/.131/.132`）— 純巧合。**

**這個反例不否定假設**，只是說明 **M13 該操作員當時沒有輸入特定編號**（不是系統不支援）。

## 從 LOG 能拿到 vs 不能拿到（修訂版）

| 想做的事 | 從 LOG 能不能做？ |
|---------|------------------|
| 知道現在在跑 O2604116 工單 | ✅ TX1 LOAD + Drive col5 |
| 知道板**面**（.T / .B） | ✅ 工單檔名後綴 |
| 知道現在用哪個工具庫位置 | ✅ MACRO R / Drive col8 |
| 知道 sub-program 切換時間點 | ✅ MACRO BLK(1) 重置 |
| 知道現在跑的是哪片**子板**（A/B/C/D/E/F/G） | ⚠️ **部分可行** — TX1 INPUT 有特定 O 編號時可解（需 MES 提供 O 編號 ↔ 板別對映表） |
| 自動化跨所有機台/操作員可靠識別 | ❌ 操作員行為不一致，覆蓋率未知 |

## 對 dashboard 規劃的意義

之前 [work_detail_fields_plan.md](work_detail_fields_plan.md) 把「板號」歸類為「需連 MES 主機」— 這次調查發現**部分機台從 LOG 也能拿到**，但需要：

1. MES 提供「O 編號 → 板別」對映規則（這在 NC 表 Excel 內，操作員/工程端有）
2. Parser 加入「TX1 INPUT [O###] 解析 → 推導當前板別」邏輯
3. 涵蓋率報告：哪些機台/班別有 INPUT 事件、哪些沒有
4. Fallback：沒有 INPUT 事件時，顯示「板別未知」（不是錯誤資料）

---
*建立日期：2026-04-30*
*4/30 晚間更新 #1：照片證據 + M03/M12/M18 實證推翻第一版「LOG 完全無資訊」結論*
*4/30 晚間更新 #2：用 Excel 解析的完整 NC 表編號 + M13 全歷史 TX1 比對 → 覆蓋率 <5%，需改 SOP*
*狀態：方向確認；技術上可解；卡在操作員行為不一致 → 真正瓶頸是 SOP 而非 LOG*

# 交付給 VS Code Claude Code 的文件清單

## 必須給的文件（3 個）

### 1. DEV_GUIDE.md ⭐ 最重要
**原因**：這是 VS Code Claude Code 的「唯一真相來源」。包含了：
- Drive.Log 的 23 欄完整定義和範例行 → parser 怎麼寫
- SQLite schema 5 張表 → 資料庫怎麼建
- HTTP API 4 個端點 + JSON 範例 → server 怎麼寫
- 計算邏輯（稼動率公式、孔數差值、跨夜處理）→ 業務邏輯怎麼算
- Golden test 驗證數據（3/17 每小時真實數字）→ 怎麼驗證正確性
- 目錄結構 → 專案怎麼組織
- 當前限制（只有 M13/M14 連線、stdlib only）→ 避免踩坑

沒有這個文件，VS Code Claude Code 完全不知道要開發什麼。

### 2. drill_dashboard.html ⭐ 前端參考
**原因**：這是已經設計好的前端 UI，經過 7 次迭代確認。VS Code Claude Code 需要它來：
- 理解 4 個 Tab 的 UI 結構和資料需求
- 知道 fetch 哪些 API 端點
- 直接在此基礎上改為 fetch 真實 API（替換 mock 數據）
- 保持 UI 設計的一致性（不需要重新設計）

沒有這個文件，VS Code Claude Code 會自己發明一套 UI，跟我們迭代 7 次的設計不一致。

### 3. VSCODE_PROMPT.md 裡的 prompt ⭐ 開發指令
**原因**：告訴 VS Code Claude Code：
- 按什麼順序開發（Step 1~6，有依賴關係）
- 嚴格遵守什麼規則（stdlib only、不綁 0.0.0.0 等）
- 品質要求（先寫測試、要有 docstring、錯誤處理）

沒有這個 prompt，VS Code Claude Code 會自己決定開發順序和風格，可能用 Flask 寫 API、用 pip install 裝套件。

---

## 不需要給的文件

| 文件 | 為什麼不用給 |
|------|-------------|
| 網路架構 PDF | 網路是硬體部署問題，跟寫程式無關。DEV_GUIDE 裡的 IP 表已足夠 |
| 老闆簡報 PDF | 商業決策文件，跟開發無關 |
| laser_drill_log_questionnaire.md | Kataoka 還沒填寫，VS Code Claude Code 不需要猜測格式 |
| test_parser_accuracy.py | Golden data 已經寫在 DEV_GUIDE 第 12 節，VS Code Claude Code 會自己寫測試 |
| 部署步驟 checklist PDF | 部署是後續工作，開發階段不需要 |

---

## 操作步驟

```
1. 在你的開發電腦上建立專案目錄：
   mkdir drill-monitoring
   cd drill-monitoring

2. 把以下文件放進去：
   drill-monitoring/
   ├── DEV_GUIDE.md
   └── web/
       └── dashboard.html

3. 用 VS Code 打開這個目錄

4. 開啟 Claude Code（Ctrl+Shift+P → Claude Code）

5. 貼入 VSCODE_PROMPT.md 裡的 prompt

6. VS Code Claude Code 會先讀 DEV_GUIDE.md，然後開始 Step 1
```

---

## 開發完成後帶回運算主機

開發完成後，整個 `drill-monitoring/` 資料夾用 USB 拷貝到運算主機上。
在運算主機上執行：

```
python main.py
```

瀏覽器打開 `http://{office_NIC_IP}:8080` 即可看到 dashboard。

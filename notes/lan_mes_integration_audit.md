# 區域網路 + MES 整合 — 現況配置與資安稽核請求

> 給審稽單位的文件。內容是「目前已知的部署配置」與「整合需求」，請就資安角度給意見：哪些是高風險、哪些可接受、應加哪些補強。沒有結論的部分請標示「待確認」。

## 1. 系統角色

| 角色 | 主機 | 作業系統 | 網路位置 | 用途 |
|---|---|---|---|---|
| 運算電腦 | 10.10.1.2 | Windows | 工廠內網 (10.10.1.0/24)，目前**無對外網路** | 跑 drill_monitor：robocopy 抓 LOG、parser 解析、SQLite 儲存、HTTP dashboard。詳見 [README](../deploy/DEPLOY_README.md) |
| 控制電腦（鑽孔機）M01–M18 | 10.10.1.11–28 | Windows（廠商鎖死） | 工廠內網，**離線**（無 NTP、無網路更新） | 機台控制；對外開 SMB 共用 `LOG` 給運算電腦讀 |
| 控制電腦（雷鑽）L1–L4 | 10.10.1.31–34 | Windows（廠商鎖死） | 同上 | 雷鑽控制；SMB 共用 `LOG` + `INFO`（L1 沒開 INFO） |
| MES 電腦 | 待確認 | 待確認 | 辦公室網路 | 排程系統，需要拉運算電腦的稼動率與工單資料 |
| 看板螢幕 | 辦公室 / 主管位 | — | 辦公室網路 | 需 HTTP 連到運算電腦的 dashboard (`:8080`) |

## 2. 目前的網路 / 認證配置

### 運算電腦 → 控制電腦（已運作）
- 連線方式：SMB (`\\10.10.1.x\LOG`)
- 認證：明文帳密寫在 `config/machines.json`（`smb_user="Takeuchi"`, 雷鑽 `Guest`，密碼空字串）
- 啟動腳本：[deploy/SETUP_NET_USE.bat](../deploy/SETUP_NET_USE.bat) 用 `net use` 把每台機台 mount 起來
- 通訊協定：SMB v1 / v2（廠商機台限制，待確認可否升 v3）

### 運算電腦 dashboard（HTTP）
- Port: TCP 8080
- 認證：**目前無認證**（任何能連到 `:8080` 的人都能看／改）
- 寫入端點：API server 大部分是讀，但 `/api/*` 還沒完整盤點是否有寫入端點
- TLS：無（純 HTTP）
- Code: [server/api_server.py](../server/api_server.py)

### 自動啟動
- 運算電腦：HKCU Run key 指到 `C:\DrillMonitor\start_monitor.bat`，Windows 已設自動登入（明文密碼放在登錄檔）
- 沒有 service / Task Scheduler，純 user-mode 程序

## 3. 規劃中的整合（需要本次稽核）

### 目標
1. 辦公室主管能用瀏覽器看運算電腦的 dashboard
2. MES 能拉運算電腦的稼動率 / 工單 / 機台狀態，做自動排程
3. **不希望** MES 或辦公室網路能直接連到鑽孔機 / 雷鑽控制電腦

### 預計改動
1. 運算電腦加上**第二張 NIC**（或現有 NIC dual IP），同時連工廠內網（10.10.1.0/24）與辦公室網路
2. 辦公室網路那側：開 TCP 8080 給辦公室子網段
3. MES 取資料的方式（待決定）：
   - **方案 A**：MES 拉 — 運算電腦多開一個 read-only API endpoint（HTTPS + token）
   - **方案 B**：運算電腦推 — 定時把 SQLite 資料 ETL 推到 MES（運算電腦主動連 outbound）
   - **方案 C**：共用檔案 — SQLite 檔案分享（read-only），MES 直接讀
4. 控制電腦：**不動**（保持與 MES / 辦公室網路隔離）

## 4. 已知的資安問題（自評）

| 問題 | 嚴重度 | 現況 |
|---|---|---|
| Dashboard 無認證 | 高 | 內網單機時可接受，接上辦公室網路後**必須**加認證 |
| HTTP 而非 HTTPS | 中 | 內網敏感度低，但跨網段後建議 TLS |
| SMB 帳密明文寫在 JSON | 中 | 讀取權限限定運算電腦本機，但 git repo 也有複本（需確認 .gitignore / 歷史） |
| 運算電腦 Windows 自動登入 | 中 | 物理存取等於完全控制 |
| 控制電腦 Guest / 空密碼 SMB | 高 | 廠商預設，無法改；接上多網段後等於把 LOG share 暴露給辦公室網路（除非 NIC 設定隔離） |
| 沒有 audit log | 中 | API server 不記錄 client IP / request；無從追溯異常存取 |
| SQLite 沒備份 | 中 | 唯一的歷史資料，硬碟壞掉就沒了 |
| 自動更新 / patch 機制 | 低 | 運算電腦目前手動 deploy；無 OS / Python 自動更新 |

## 5. 想請審稽單位回答的問題

1. **NIC 隔離設計** — 運算電腦同時連兩個網段，怎麼確保 MES / 辦公室不能透過運算電腦 routing 到 10.10.1.x 的控制電腦？要不要加防火牆規則 / 禁止 IP forwarding？
2. **Dashboard 認證** — BASIC auth 夠嗎？還是要 reverse proxy + SSO？看板螢幕（kiosk 模式）怎麼處理？
3. **MES 整合三方案的取捨** — 從資安角度哪個最佳？
4. **SMB 明文密碼** — 廠商鎖死沒辦法改，怎麼補強？（網段隔離？SMB 簽章強制？）
5. **資料保留與備份** — SQLite 多久備份一次？備份要存哪？備份本身的存取控制？
6. **異常存取偵測** — 要不要在 API server 加 audit log + 速率限制？是否需要把 log 送到中央 SIEM？
7. **OS 更新策略** — 運算電腦能不能上 WSUS 或定期 patch？控制電腦廠商鎖死是否能說服廠商開放？

## 6. 參考資料

- 部署文件：[deploy/DEPLOY_README.md](../deploy/DEPLOY_README.md)
- 切換上線步驟：[deploy/CUTOVER_STEPS.md](../deploy/CUTOVER_STEPS.md)
- 機台清單：[config/machines.json](../config/machines.json)
- 系統概觀：[CLAUDE.md](../CLAUDE.md)

---

**填寫者**：Ryan
**日期**：2026-04-30
**狀態**：草稿，等資安回覆後再決定 NIC + 認證的最終配置

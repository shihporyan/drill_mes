# SMB Stale Session After Power Loss (2026-05-07)

跳電 → 運算電腦 (10.10.1.2) 自動重開 → main.py 自動跑了 → 但 dashboard 顯示
M01/M02/M13/M14 時間不對。實際排查後發現 M01/M13/M14 是 SMB session
壞了，M02 是另一個問題（可能是 RTC 飄）。

## 症狀

- ping 四台 control PC 都通 (latency < 1ms)
- `dir \\10.10.1.11\LOG` (M01) → **「使用者名稱或密碼不正確」**
- `dir \\10.10.1.12\LOG` (M02) → 通，檔案 mtime 是當天上午
- `dir \\10.10.1.23\LOG` (M13) → **「使用者名稱或密碼不正確」**
- `dir \\10.10.1.24\LOG` (M14) → **「使用者名稱或密碼不正確」**

四台用的是**完全一樣的 Takeuchi 帳號 + 空密碼**，所以不是帳密真的錯。

## 根因

跳電瞬間，Windows 這邊的 SMB session 是「半開」狀態被砍掉。Reboot 後
Credential Manager 嘗試自動重連 persistent mapping，重連失敗後**不會把
mapping 砍掉**，而是留在 "Disconnected" 狀態，**cache 一個壞掉的 NTLM
token**。後續任何戳這個路徑的請求都用那個壞 token 去 auth，對方 SMB
回 auth fail。

關鍵診斷點：訊息是「使用者名稱或密碼不正確」**不是**「找不到網路路徑」。
- 「找不到網路路徑」= 對方 SMB 不通（PC 沒開、port 被擋）
- 「使用者名稱或密碼不正確」= 對方 SMB 活著回 auth challenge，但我這邊
   給的 token 被拒 → 99% 是 stale session

M02 為什麼 OK 是 race condition — 它的舊 token 過期 timing 剛好對上 / 重
連順序剛好對。**不可預測，不要假設特定機台容易壞**。

## 為什麼 boot 自動重綁沒救起來

`deploy/start_monitor.bat` 在 reboot 後 sleep 180s 再重 issue
`net use ... /persistent:yes`，**但漏了一個關鍵 step：先 delete 舊 mapping**。

當 mapping name 已存在（即使是 disconnected 狀態），`net use` 會回
「已經有與本地裝置的連線」然後 **no-op**。所以重綁等於沒做。

對照 `deploy/SETUP_NET_USE.bat`，那個有 "Re-run safe" 設計（先全部
delete 再建立），所以一次性 setup 沒問題；但 boot 自動 script 漏了。

## 為什麼沒被即時發現

1. `collector/log_collector.py` 的 backoff 機制 — 連續 3 次 robocopy 失
   敗後，那台機台變成每 30 分鐘才 retry。`machine_health.is_online=0`
   有寫進 DB 但沒人主動看。
2. Dashboard `web/dashboard.html:73` 把 `last_seen` 寫死 `null`，介面上
   沒「最後成功讀取時間」欄位，看到的只是「資料舊舊的」而不是「離線 X
   分鐘」。

→ 三台機台已經斷了不知道幾小時，操作員只覺得「介面上的時間看起來怪
怪的」，沒意識到是失聯。

## 手動修復步驟（事件當下用的）

```cmd
REM 先看狀態
net use

REM 清掉壞 mapping（包含可能殘留的 IPC$ session）
net use \\10.10.1.11\LOG /delete /yes
net use \\10.10.1.11     /delete /yes
net use \\10.10.1.23\LOG /delete /yes
net use \\10.10.1.23     /delete /yes
net use \\10.10.1.24\LOG /delete /yes
net use \\10.10.1.24     /delete /yes

REM 重綁
net use \\10.10.1.11\LOG "" /user:Takeuchi /persistent:yes
net use \\10.10.1.23\LOG "" /user:Takeuchi /persistent:yes
net use \\10.10.1.24\LOG "" /user:Takeuchi /persistent:yes

REM 驗證
dir \\10.10.1.11\LOG
dir \\10.10.1.23\LOG
dir \\10.10.1.24\LOG
```

驗證通過。

## 永久修法（已 patch）

**P0-1 + P0-2** — `deploy/start_monitor.bat`
- Boot 流程現在先 `net use /delete /yes` 清乾淨（22 台 + bare IP），
  再重綁。
- 重綁後跑 `python -m collector.health_check`，輸出寫到
  `C:\DrillMonitor\smb_boot_check.log`，掃到 "OFFLINE" 就 console
  印 WARNING + 停 10s 讓人看到。

**P2-5** — `collector/log_collector.py`
- 新增 `remount_smb_share(machine, machines_config)` helper：force
  delete + 重綁，timeout 防卡死。
- `run_collection_cycle` 收到 robocopy 失敗時，先 re-mount + retry
  一次再算數。runtime 中 SMB 抖動就會自動恢復，不用等 30min backoff。

## 暫不做（已討論決定）

**P1 — Dashboard 加「資料新鮮度」欄位**：操作面但目前管理流程能接受，
延後。如果未來再發生類似事件被現場誤判，優先補這個。

**P2-6 — 改用 `smbprotocol` 不依賴 net use**：徹底解法但要重寫 collector
+ 跑跨環境測試，工程量大。先靠 P0+P2-5 的雙保險。

## 下次同樣症狀的快速判斷流程

1. `dir \\IP\LOG` 失敗訊息看是哪一種：
   - 「找不到網路路徑」→ 對方 PC 沒開 / 網路斷 → 去機台旁邊看螢幕
   - 「使用者名稱或密碼不正確」→ stale session → 直接 delete + 重綁
2. **不要懷疑帳密真的錯**（除非有別人動過 control PC 的帳號）
3. 以後 boot 完應該不會再遇到這狀況（patch 已修），但如果 runtime
   中 SMB 抖動，parser 會自動嘗試 remount + retry — 看 `drill_monitor.log`
   裡有沒有 `Attempting SMB re-mount and retry` 訊息。

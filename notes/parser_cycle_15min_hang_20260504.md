# Parser Cycle 15 分鐘卡住事件 (2026-05-04 09:10~09:25)

## 現象

5/4 上午操作員在 10.10.1.2 上看到瀏覽器顯示「**連線被拒 (connection refused)**」，
切過去看 cmd 視窗也沒在動。

時間軸：
- `09:10:21~22` parser cycle 跑完 M01-M18 的 `parse_drive`（1 秒內）
- `09:10:22` `parse_tx1` 開始（log 印出 `TX1 parser cycle sta...` 後就停了）
- `09:25:46` `parse_laser` 結束、cleanup 完、cycle 結束
- 終端機印出：
  ```
  WARNING drill_monitor: Cycle took 944216ms, longer than poll_interval 300s.
  ```
  944216ms ≈ **15.7 分鐘**，遠超 5 分鐘預算

## 表象 vs 根因

操作員第一眼看到的是 cmd 視窗有大段 traceback：
```
ConnectionAbortedError: [WinError 10053] 連線已被您主機上的軟體中止。
File "C:\DrillMonitor\server\api_server.py", line 414, in _handle_overview
```

**這不是原因，是症狀**：
1. parser cycle 跑了 15 分鐘 → 期間 parser 抓著 SQLite write lock / SMB I/O 吃滿
2. `/api/overview` SELECT 排隊等，回應變慢
3. 瀏覽器預設 ~30s timeout，client 先放棄關 socket
4. server 終於想送 response 時 `sock.sendall()` 失敗 → WinError 10053

換句話說：**cycle 太慢 → API 慢 → 瀏覽器斷線 → server 印 traceback**。
Traceback 在 log 看起來嚇人，但不是 server 死掉，process 還活著。

## 另一個小插曲：Windows console mark mode

第一次看截圖時誤以為是 process 還活著但 stdout 被凍住——
原因是 cmd 視窗標題列有「**選取**」前綴：
```
選取 系統管理員: DrillMonitor
```
這是 Windows console 的 mark/selection 模式，不小心點到視窗會觸發，
會 block stdout write 直到按 ESC / Enter / 右鍵。

這次事件**不是** mark mode（後來證實是真的 cycle 跑很久），但**這個陷阱真的存在**。
建議：
- 用 Windows Terminal 替代傳統 cmd（mark mode 行為不同，比較不會誤觸）
- 或在 `start_monitor.bat` 加 stdout redirect：`python main.py >> drill_monitor.log 2>&1`
- 或把 main.py 包成 Windows service（NSSM）完全脫離 console

## 為什麼 cycle 會跑 15 分鐘

從時間軸推算：`parse_tx1` + `parse_laser` 兩步合計吃掉 ~15.4 分鐘
（`parse_drive` 1 秒、collect 階段在那之前也很快）。

**還沒確認**是哪一步、哪一台機台拖累。可能性：
- **parse_laser**: L1 廠商還沒修好，SMB timeout 可能很長
  （memory: `project_laser_smb_setup` — L1 待廠商修）
- **parse_tx1**: Takeuchi SMB 讀大檔
  （memory: `project_smb_lazy_mtime` — M01/M03/M17 SMB 讀取會卡）

下一次發生時直接看 `Cycle step timings:` log 就有答案（見下方修改）。

## 已做的改動 (5/8)

### 1. Per-step 耗時 log — `main.py:109-125`

每個 step 用 `time.monotonic()` 量測，cycle 結束印一行：
```
Cycle step timings: collect_takeuchi=12345ms, collect_laser=6789ms,
                    parse_drive=1234ms, parse_tx1=520000ms, parse_laser=400000ms,
                    cleanup=10ms
```

### 2. WAL mode 保險 — `db/init_db.py:80`

WAL 已在 `parsers/base_parser.py:126` 設過，且 WAL 是 DB 級持久化設定（設一次就永久）。
現在 init_db 也設一次當保險，避免新 DB 第一次 parser 跑之前的空窗期沒 WAL。

WAL 的作用：reader 不被 writer 阻塞、reverse 也不阻塞。
理論上 parser 跑 15 分鐘時 `/api/overview` 應該還能讀。
但 WinError 10053 仍可能因「parser 在同一 process 把 CPU/SMB I/O 吃滿，回應慢到 browser timeout」發生
——WAL 解決鎖、不解決資源競爭。

### 3. 部署到 production

照 memory `feedback_deploy_settings` 流程，手動把 `main.py` 和 `db/init_db.py` 拷到 `C:\DrillMonitor\`，
重啟 service。確認 WAL 生效：DB 檔同目錄會出現 `*.db-wal` 和 `*.db-shm`。

## 下一次發生時要做什麼

1. **不要急著重啟**——先在 log 找最近一筆 `Cycle step timings:`，看哪一步最慢
2. 若是 `parse_laser` → 看 L1/L2/L3/L4 哪一台 SMB error 多
3. 若是 `parse_tx1` → 看 M13~M18 哪一台讀檔卡住
4. 對照 SMB 連線狀態：`net use` 看 mapping 是否還在；參考 `smb_stale_session_after_power_loss.md`

## 待解問題（下一輪）

- [ ] 確認 5/4 之後是否還有 cycle >300s（看 `cycle_stats` table）
- [ ] 找出 parse_tx1 / parse_laser 真正耗時的根因（per-machine 計時可能要再加）
- [ ] 評估是否需要把 parser 從 main process 拆出來，避免吃滿同 process 影響 API

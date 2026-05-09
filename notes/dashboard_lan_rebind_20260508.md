# Dashboard 改綁 NIC2 (LAN/MES 整合第一步) — 2026-05-08 下午執行

## 目標

讓辦公網（192.168.2.0/24）的主管 PC 能看 dashboard，但 OT 網段（10.10.1.0/24）的控制電腦看不到也連不到 dashboard。

```
現況: dashboard bind 10.10.1.2:8080 (NIC1, 只有 OT 看得到)
目標: dashboard bind 192.168.2.50:8080 (NIC2, 辦公網看得到)
```

## 實作（4 步）

| # | 動作 | 細節 |
|---|------|------|
| 1 | 改 `config/settings.json` 的 `http_host` | 從 `10.10.1.2` 改成 `192.168.2.50` |
| 2 | 重啟 dashboard | kill 既有 python + 跑 `C:\DrillMonitor\start_monitor.bat`（**沒有 NSSM**，見下方 caveat） |
| 3 | 本機驗證 | 在 10.10.1.2 上開瀏覽器 `http://192.168.2.50:8080`（不是 localhost — 因為 bind 不是 0.0.0.0） |
| 4 | 主管 PC 驗證 | `http://192.168.2.50:8080` 應直接看到 dashboard |

## 程式碼確認（5/8 已查）

- `config/settings.json` 既有 `"http_host": "TODO_OFFICE_NIC_IP"` placeholder，rebind = 改一行
- `server/api_server.py:1049-1059` 已支援讀 `http_host`，且明確拒絕 `0.0.0.0`（fallback 127.0.0.1）→ 必須填具體 IP
- 改 IP 後不會自動跑 `deploy/start_monitor.bat`，要手動重啟

## Caveat — 不可全信外部對話的兩點

### 1. 「NSSM 那層重啟即可」→ ❌ 沒有 NSSM

實際的 production 啟動機制（見 memory `project_production_autostart`）：
- HKCU Run key 指向 `C:\DrillMonitor\start_monitor.bat`
- Windows 自動登入 → 開機自動跑 batch
- 無 service / Task Scheduler

**重啟方式：**
```cmd
REM 找 python 殺掉
tasklist | findstr python
taskkill /F /PID <pid>

REM 重跑 batch（會 setup net use + 啟動 main.py）
C:\DrillMonitor\start_monitor.bat
```

### 2. 「dashboard_allowed_networks 在 VS Code 規劃中」→ ❌ 沒有

外部對話提到「方法 B — 應用層：在 Dashboard Python 程式碼裡實作 IP 白名單，dashboard_allowed_networks 那個欄位等你填」。

**現況：** code 裡完全沒有 `dashboard_allowed_networks` / `allowed_networks` / whitelist 邏輯（5/8 grep 過 server/、deploy/、config/）。要做要從零實作。

→ 5/8 下午先不要靠這層；安全只靠**防火牆規則 + bind 在 NIC2**。

## 資安層（已決策）

| 控制 | 機制 | 狀態 |
|------|------|------|
| 防外網看到 dashboard | 辦公網 air-gap | 已存在 |
| 防 OT 機台看到 dashboard | bind 在 NIC2，OT 連不到 192.168.2.50 | 本次設定 |
| 防 IP forwarding（辦公網→OT） | `IPEnableRouter=0` + 防火牆 `Block_NIC2_to_OT` | 早上已設 |
| 防火牆允許辦公網存取 8080 | `Allow_Dashboard_8080`，RemoteAddress=192.168.2.0/24 | 早上已設 |
| Dashboard 認證 | 無（v3 §6.5 已決策不加密碼） | 不做，TODO phase-3 |

**進階加分項（暫不做）：**
- 把防火牆 `Allow_Dashboard_8080` 的 RemoteAddress 從整個 /24 收緊到具體主管 PC IP 清單
- 應用層 IP 白名單（需新寫 code）

## 完整資安評估參考

- [鑽孔機監控系統_資安評估報告_v3_4_2026-05-08.pdf](../doc/鑽孔機監控系統_資安評估報告_v3_4_2026-05-08.pdf)
- [鑽孔機監控系統_資安評估報告_圖組.pdf](../doc/鑽孔機監控系統_資安評估報告_圖組.pdf)

## 連結到舊 audit doc

[lan_mes_integration_audit.md](lan_mes_integration_audit.md) 4/30 草稿的「3. 規劃中的整合」對應這次 NIC2 dashboard 重綁。MES 整合方案 A/B/C 還未決定（不在本次 5/8 工作範圍）。

## 驗收 checklist

- [ ] `config/settings.json` `http_host` = `192.168.2.50`
- [ ] python process 已重啟，log 印 `API server starting on http://192.168.2.50:8080`
- [ ] 10.10.1.2 本機瀏覽器 `http://192.168.2.50:8080` 通
- [ ] 主管 PC `http://192.168.2.50:8080` 通
- [ ] OT 機台側（10.10.1.x 任一台）`telnet 192.168.2.50 8080` 不通（route 不存在 / 防火牆擋）
- [ ] OT 機台側 `telnet 10.10.1.2 8080` 不通（dashboard 不再 bind 在 NIC1）

## 異常排除

- bind 失敗 `[Errno 49] Can't assign requested address` → 192.168.2.50 不是這台機的 IP，跑 `ipconfig` 確認 NIC2 真的有這個 IP
- 主管 PC 連不到 → 先 ping 192.168.2.50 看路由通不通；再看防火牆 inbound 規則 `Allow_Dashboard_8080` 是否 enabled
- 本機連 localhost:8080 不通是**正常的**（bind 不是 0.0.0.0，localhost=127.0.0.1 ≠ 192.168.2.50）

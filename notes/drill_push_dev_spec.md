# 鑽孔端推送 MES — 開發規格 / 交辦文件

**對象**：鑽孔監控系統開發者 / 維運者
**目的**：在鑽孔端建立每日自動推送 CSV 到 MES 的機制
**日期**：2026-05-12

---

## 1. 背景

### 1.1 原本計畫（已放棄）
原本要用 Windows SMB 共用：鑽孔端把 CSV 寫到 MES 主機分享出來的網路資料夾。

### 1.2 為什麼放棄
兩台都是 Win 11 26200，遇到 NTLM/SMB session 驗證在 IPv4 路徑下被靜默拒絕（Status 0xC000006D / SubStatus 0x0），歷經以下嘗試**全部無解**：

- 換密碼、改用純 ASCII / 純數字
- 改鑽孔端電腦名（原本兩台都是 DESKTOP-B59E81I 同名）
- 鬆綁 `NtlmMinServerSec`
- 啟用 SMB server 簽署（`EnableSecuritySignature=True`）
- 開啟共用層 SMB 加密（`EncryptData=True` on share）
- `LocalAccountTokenFilterPolicy = 1`
- 修補 `drill_writer` 的 `PasswordRequired` 旗標
- 多種 user 寫法（`drill_writer` / `DESKTOP-B59E81I\drill_writer` / `192.168.2.211\drill_writer`）
- `cmdkey /generic:` 預存帳密

**奇特現象**：MES 自己 PowerShell 透過 IPv4 連自己會通；鑽孔端透過 IPv4 來就死，且只有 IPv6 link-local 連線會成功。問題判斷在 Win11 26200 SMB session setup 層的某個強化政策，但無法定位是哪一條。

### 1.3 新方案
**HTTP POST 推送**：在 MES 跑一支獨立的 Python HTTP 服務（port 8081），鑽孔端用 `curl` 把 CSV POST 過去。完全繞開 Windows SMB/NTLM。

---

## 2. MES 端現況（已設定好，不需要動）

| 項目 | 值 |
|---|---|
| MES 主機 IP | `192.168.2.211` |
| 接收服務 URL | `http://192.168.2.211:8081` |
| 接收服務名 | `mes_drill_receiver.py` |
| 落地資料夾（MES 端）| `D:\drill_export\` |
| 防火牆 | 只允許 `192.168.2.50` 進入 TCP 8081 |
| 認證 | Token + IP 白名單 雙重 |
| 開機自啟動 | 工作排程器 `MES_DrillReceiver`（待安排，預計下班時間部署）|

> ⚠️ 目前 MES 接收服務是**手動前景跑**的（為避免下午影響生產），開機自啟動會在下班後設定。鑽孔端開發測試期間，請先跟 MES 端負責人協調確認服務是否在跑（用 `/health` 端點檢查即可）。

---

## 3. HTTP API 規格

### 3.1 健康檢查（不需認證）

```
GET /health
```

**回應 200**：
```json
{
  "status": "ok",
  "service": "mes_drill_receiver",
  "version": "1.0",
  "time": "2026-05-12T14:30:00",
  "drop_folder": "D:\\drill_export"
}
```

用途：在動真槍實彈推 CSV 之前，先用這個確認網路 + 防火牆 + 服務都通。

### 3.2 上傳檔案

```
POST /upload/<filename>
Headers:
  X-Token: <TOKEN>
Body: CSV 檔內容 (raw bytes, Content-Type 不重要)
```

也支援 `PUT /upload/<filename>`（等價）。

**成功回應 200**：
```json
{
  "status": "ok",
  "filename": "drill_2026-05-12.csv",
  "bytes": 12345,
  "saved_to": "D:\\drill_export\\drill_2026-05-12.csv",
  "received_at": "2026-05-12T23:00:01"
}
```

**錯誤回應**：HTTP 狀態 + JSON `{"error": "<code>", ...}`，詳見 §3.5 錯誤碼表。

### 3.3 限制

| 項目 | 限制 |
|---|---|
| 檔名字元 | `[A-Za-z0-9_\-.]` |
| 檔名長度 | ≤ 128 字元 |
| 副檔名 | `.csv` `.txt` `.json`（白名單）|
| 單檔大小 | ≤ 50 MB |
| 同檔名 | 預設覆蓋（可改 config 設為拒絕）|
| 來源 IP | 白名單 `192.168.2.50`（鑽孔端）+ 127.0.0.1 |

### 3.4 範例（cmd / PowerShell）

```bat
curl -X POST ^
  -H "X-Token: REPLACE_WITH_TOKEN" ^
  --data-binary "@D:\drill_data\drill_2026-05-12.csv" ^
  http://192.168.2.211:8081/upload/drill_2026-05-12.csv
```

### 3.5 錯誤碼表

| HTTP | error code | 意義 | 排查方向 |
|---|---|---|---|
| 200 | - | 成功 | - |
| 400 | `bad_path` | URL path 不是 `/upload/<filename>` 格式 | 檢查 URL 結尾有沒有 `/upload/<檔名>` |
| 400 | `bad_filename` | 檔名含非法字元 | 改用 `[A-Za-z0-9_\-.]` |
| 400 | `bad_extension` | 副檔名不允許 | 改 `.csv` `.txt` `.json` 或請 MES 端加 |
| 400 | `empty_body` | 沒帶檔案內容 | 確認 `--data-binary @file` 的 file 存在且非空 |
| 400 | `truncated` | 傳一半斷線 | 重試；檢查網路穩定性 |
| 401 | `bad_token` | Token 錯 | 跟 MES 端核對 token |
| 403 | `ip_not_allowed` | 來源 IP 不在白名單 | 檢查鑽孔端是不是 192.168.2.50 |
| 404 | `not_found` | 不存在的 endpoint | 檢查 URL |
| 409 | `file_exists` | 同檔名已存在且不允許覆蓋 | 改檔名或請 MES 端設 `overwrite_allowed: true` |
| 413 | `too_large` | 超過 50 MB | 拆檔 / 壓縮 / 請 MES 端調大上限 |
| 500 | `write_failed` | MES 端寫檔失敗 | 找 MES 端查 `D:\drill_export\receiver.log` |

---

## 4. Token 取得

**請用安全管道**（口頭、簽紙、USB 隨身碟拷檔、加密訊息）跟 MES 端負責人取得 token。

Token 特性（目前產的這版）：
- 長度 32 字元
- 只含 `[A-Za-z0-9]`，且**排除了** `0`、`1`、`I`、`O`、`l`（避免視覺混淆）
- 範例（**不是真的 token**）：`G7pYqRm5sDfVnZjT9cBxW4Hg2aLuKePr`

> ⚠️ Token 不要寫進：email、LINE、git commit、原始碼中 hardcode、log 檔。
> 推薦放法：`C:\drill_push\token.txt`（一行純文字，整個檔只有 token），讓推送腳本從檔案讀。

---

## 5. 鑽孔端要做的事

### 5.1 檔案佈局

```
C:\drill_push\
├── push_drill_to_mes.bat    ← 主推送腳本（範本見 §5.2）
├── token.txt                ← MES 給的 token（一行）
└── push.log                 ← 每次推送的紀錄（執行後自動產生）
```

### 5.2 推送腳本範本（cmd / bat 版）

> ⚠️ 以下範本有幾個 **TODO** 需要鑽孔開發者依實際情況調整。

```bat
@echo off
REM ============================================================
REM MES 鑽孔資料每日推送
REM 由 Windows 工作排程器每天定時呼叫
REM ============================================================

REM ===== TODO 1: 設定區（依實際狀況調整） =====
set MES_URL=http://192.168.2.211:8081
set TOKEN_FILE=C:\drill_push\token.txt
set CSV_FOLDER=D:\drill_data
set CSV_PREFIX=drill_
set CSV_SUFFIX=.csv
set LOG_FILE=C:\drill_push\push.log

REM ===== 計算今天日期（YYYY-MM-DD，locale 中立）=====
for /f "tokens=2 delims==" %%i in ('wmic os get localdatetime /value 2^>nul ^| find "="') do set DT=%%i
set YYYY=%DT:~0,4%
set MM=%DT:~4,2%
set DD=%DT:~6,2%
set TODAY=%YYYY%-%MM%-%DD%

set CSV_NAME=%CSV_PREFIX%%TODAY%%CSV_SUFFIX%
set CSV_PATH=%CSV_FOLDER%\%CSV_NAME%

echo. >> "%LOG_FILE%"
echo [%date% %time%] === push start === >> "%LOG_FILE%"
echo [%date% %time%] CSV: %CSV_PATH% >> "%LOG_FILE%"

REM ===== 驗證 CSV 檔存在 =====
if not exist "%CSV_PATH%" (
  echo [%date% %time%] ERROR exit=2: CSV not found >> "%LOG_FILE%"
  exit /b 2
)

REM ===== 驗證 token 檔 =====
if not exist "%TOKEN_FILE%" (
  echo [%date% %time%] ERROR exit=3: token file missing %TOKEN_FILE% >> "%LOG_FILE%"
  exit /b 3
)
set /p TOKEN=<"%TOKEN_FILE%"
if "%TOKEN%"=="" (
  echo [%date% %time%] ERROR exit=4: token file empty >> "%LOG_FILE%"
  exit /b 4
)

REM ===== 推送（-s 靜默、-w 取回 HTTP code、-o 把回應存到檔）=====
curl -s -S ^
  -X POST ^
  -H "X-Token: %TOKEN%" ^
  --data-binary "@%CSV_PATH%" ^
  -o "%LOG_FILE%.last_response.json" ^
  -w "[%%{time_local}] HTTP %%{http_code} upload_bytes=%%{size_upload} time=%%{time_total}s" ^
  "%MES_URL%/upload/%CSV_NAME%" >> "%LOG_FILE%" 2>>"%LOG_FILE%"

set CURL_EXIT=%errorlevel%
echo. >> "%LOG_FILE%"

if not %CURL_EXIT%==0 (
  echo [%date% %time%] ERROR exit=1: curl errorlevel=%CURL_EXIT% >> "%LOG_FILE%"
  exit /b 1
)

REM ===== 驗證 MES 回應確實是 200 =====
findstr /C:"HTTP 200" "%LOG_FILE%" >nul 2>&1
REM 上面這行不算精準（會找到歷史紀錄），改用判斷 last_response.json 內容比較好
findstr /C:"\"status\": \"ok\"" "%LOG_FILE%.last_response.json" >nul 2>&1
if errorlevel 1 (
  echo [%date% %time%] ERROR exit=5: MES rejected, see last_response.json >> "%LOG_FILE%"
  exit /b 5
)

echo [%date% %time%] === push end OK === >> "%LOG_FILE%"
exit /b 0
```

**Exit code 對照**：

| Exit | 意義 |
|---|---|
| 0 | 成功 |
| 1 | curl 執行失敗（網路不通、DNS 等）|
| 2 | 今日 CSV 檔不存在 |
| 3 | token 檔不存在 |
| 4 | token 檔空 |
| 5 | curl 成功但 MES 拒絕（看 last_response.json 找 error code）|

### 5.3 推送腳本範本（Python 版，替代方案）

如果鑽孔端比較熟 Python（或 .bat 寫得不順），可以用以下版本：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
鑽孔每日推送到 MES (Python 版)
與 push_drill_to_mes.bat 等價
"""
import json
import logging
import sys
import urllib.request
from datetime import date
from pathlib import Path

# ===== TODO 1: 設定區 =====
MES_URL = "http://192.168.2.211:8081"
TOKEN_FILE = Path(r"C:\drill_push\token.txt")
CSV_FOLDER = Path(r"D:\drill_data")
CSV_PREFIX = "drill_"
CSV_SUFFIX = ".csv"
LOG_FILE = Path(r"C:\drill_push\push.log")
TIMEOUT_SECONDS = 60

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8",
)
log = logging.getLogger("drill_push")

def main():
    today = date.today().isoformat()  # 2026-05-12
    csv_name = f"{CSV_PREFIX}{today}{CSV_SUFFIX}"
    csv_path = CSV_FOLDER / csv_name

    log.info("=== push start === CSV=%s", csv_path)

    if not csv_path.exists():
        log.error("CSV not found: %s", csv_path)
        return 2
    if not TOKEN_FILE.exists():
        log.error("token file missing: %s", TOKEN_FILE)
        return 3

    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        log.error("token file empty")
        return 4

    body = csv_path.read_bytes()
    url = f"{MES_URL}/upload/{csv_name}"

    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"X-Token": token, "Content-Type": "text/csv"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace") if e.fp else ""
        log.error("HTTP %d %s body=%s", e.code, e.reason, body_text)
        return 5
    except Exception as e:
        log.error("network/transport error: %r", e)
        return 1

    if status != 200:
        log.error("unexpected status=%d body=%s", status, raw)
        return 5

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log.error("non-JSON response: %s", raw)
        return 5
    if payload.get("status") != "ok":
        log.error("MES rejected: %s", payload)
        return 5

    log.info("=== push end OK === bytes=%d", payload.get("bytes", 0))
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

執行：`python C:\drill_push\push_drill_to_mes.py`

### 5.4 工作排程器（每日定時跑）

在鑽孔端**以系統管理員身分**開 PowerShell，跑：

```powershell
# ===== TODO 2: 推送時間，預設 23:00（每天晚上） =====
$pushTime = "23:00"

# bat 版本：
$action = New-ScheduledTaskAction -Execute "C:\drill_push\push_drill_to_mes.bat"

# Python 版本（擇一，註解上面那行、把下面取消註解）：
# $action = New-ScheduledTaskAction -Execute "C:\<python_path>\python.exe" -Argument "C:\drill_push\push_drill_to_mes.py"

$trigger = New-ScheduledTaskTrigger -Daily -At $pushTime

$settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
  -DontStopOnIdleEnd

$principal = New-ScheduledTaskPrincipal `
  -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName "MES_DrillPushDaily" `
  -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal `
  -Description "鑽孔資料每日推送 MES (port 8081)" -Force

# 驗證
Get-ScheduledTask -TaskName "MES_DrillPushDaily" | Format-List TaskName, State, Triggers
```

---

## 6. 測試計畫（鑽孔端開發過程依序跑）

### Test 1：網路與健康檢查

```bat
ping 192.168.2.211 -n 4
curl http://192.168.2.211:8081/health
```

**預期**：
- ping 0% loss
- /health 回 200 + JSON

### Test 2：手動單檔推送（用一個小測試檔）

```bat
echo test-from-drill-PC,%date%,%time% > C:\drill_push\test_drill.csv
type C:\drill_push\test_drill.csv

set /p TOKEN=<C:\drill_push\token.txt
curl -X POST -H "X-Token: %TOKEN%" --data-binary "@C:\drill_push\test_drill.csv" http://192.168.2.211:8081/upload/test_drill.csv
```

**預期**：回 `{"status":"ok",...}`

### Test 3：故意錯誤測試（驗證認證有作用）

```bat
:: 沒 token
curl -X POST --data-binary "@C:\drill_push\test_drill.csv" http://192.168.2.211:8081/upload/test_nooauth.csv
:: 預期：{"error":"bad_token"}

:: 錯誤副檔名
echo dummy > C:\drill_push\test.bad
curl -X POST -H "X-Token: %TOKEN%" --data-binary "@C:\drill_push\test.bad" http://192.168.2.211:8081/upload/test.bad
:: 預期：{"error":"bad_extension"}

:: 錯誤檔名（含空白）
curl -X POST -H "X-Token: %TOKEN%" --data-binary "@C:\drill_push\test_drill.csv" "http://192.168.2.211:8081/upload/has space.csv"
:: 預期：{"error":"bad_filename"}
```

### Test 4：跑一次主推送腳本

```bat
C:\drill_push\push_drill_to_mes.bat
echo errorlevel: %errorlevel%
type C:\drill_push\push.log
```

**預期**：
- errorlevel 0
- push.log 最後一行有 `=== push end OK ===`
- MES 端 `D:\drill_export\` 有今天的 CSV

> 如果今天還沒產生 CSV，先手動建一個假的測試檔，命名符合 `drill_YYYY-MM-DD.csv`，放到 `D:\drill_data\`（或你設的 CSV_FOLDER）。

### Test 5：排程器跑一次

```powershell
Start-ScheduledTask -TaskName "MES_DrillPushDaily"
Start-Sleep -Seconds 5
Get-ScheduledTask -TaskName "MES_DrillPushDaily" | Get-ScheduledTaskInfo |
  Format-List LastRunTime, LastTaskResult, NumberOfMissedRuns
type C:\drill_push\push.log
```

**預期**：
- `LastTaskResult: 0`
- push.log 又多一筆紀錄

---

## 7. TODO Checklist（鑽孔開發者填）

### 7.1 確認規格細節
- [ ] CSV 實際檔名格式：________________（範例：`drill_2026-05-12.csv` 或別的）
- [ ] CSV 來源資料夾路徑：________________（範例：`D:\drill_data`）
- [ ] 每天什麼時間推送最合適：________________（範例：23:00）
- [ ] 如果一天有多個 CSV，要全部推還是只推主檔？________________
- [ ] CSV 編碼是 UTF-8 還是 Big5 還是其他？________________

### 7.2 部署
- [ ] 建立 `C:\drill_push\` 資料夾
- [ ] 把 token 寫到 `C:\drill_push\token.txt`（一行，無換行、無空白）
- [ ] 把 `push_drill_to_mes.bat`（依 7.1 結果調整 TODO 1 設定區）放進去
- [ ] 跑 Test 1 ~ Test 5 全綠
- [ ] 註冊工作排程器（§5.4）
- [ ] 等隔天看排程器有沒有自動跑

### 7.3 移交資訊回 MES 端
- [ ] 確認推送排程時間（讓 MES 端能監看 log）
- [ ] 確認 CSV 命名規則（讓 MES 端能對應）

---

## 8. 常見問題

**Q1：跑 Test 1 ping 通，但 `/health` 連線失敗？**
- MES 端服務可能沒在跑，請聯絡 MES 端
- 防火牆規則可能漏設

**Q2：Test 2 拿到 `bad_token` 但你確定 token 沒打錯？**
- 看 `C:\drill_push\token.txt` 結尾有沒有額外的換行、空白
- notepad 編輯時最後一行不要再按 Enter
- 推薦用 `set /p TOKEN=<token.txt` 讀，這個會自動去掉換行

**Q3：Test 2 拿到 `ip_not_allowed`？**
- 鑽孔端的對外 IP 不是 192.168.2.50，請跟 MES 端確認白名單

**Q4：排程器跑了但 `LastTaskResult` 不是 0？**
- `LastTaskResult` 是 .bat 的 exit code，對照 §5.2 exit code 表
- 看 `push.log` 找最近一筆 ERROR 行

**Q5：要重推今天的、或補推昨天的？**
- 手動跑：`C:\drill_push\push_drill_to_mes.bat`
- 補推任意日：先改 .bat 計算日期那段，或臨時 curl
  ```bat
  set /p TOKEN=<C:\drill_push\token.txt
  curl -X POST -H "X-Token: %TOKEN%" --data-binary "@D:\drill_data\drill_2026-05-11.csv" http://192.168.2.211:8081/upload/drill_2026-05-11.csv
  ```

**Q6：CSV 推上去後，MES 那邊怎麼處理？**
- 目前只負責**收檔到 `D:\drill_export\`**，後續 MES 主系統怎麼讀進來、import 進資料庫，由 MES 端另外處理（不在本文件範圍）

**Q7：要怎麼確認 MES 端有收到？**
- 看 MES 端 `D:\drill_export\` 有沒有對應日期的檔
- 看 MES 端 `D:\drill_export\receiver.log` 找對應時間有沒有 `ACCEPT filename='...' from=192.168.2.50` 一行

---

## 9. 後續可擴充項目（不急，先 MVP）

- [ ] 推送失敗時自動重試 N 次（目前依賴工作排程器的 RestartCount）
- [ ] 推送成功後把 CSV 標記已推（move 到 sent/ 子資料夾或加副檔名）
- [ ] 一次推多檔（例如同時推 daily summary + detail）
- [ ] 補推前 N 天未推送的檔
- [ ] 改 HTTPS（目前 HTTP + token + IP 白名單，內網可接受；如要升級需 MES 端配合產自簽憑證）
- [ ] 把 MES 端 receiver 包成 Windows 服務（目前用工作排程器）

---

## 10. 聯絡 MES 端

- MES 主程式 / 接收服務原始碼位置：`c:\win_share\mes\mes_drill_receiver.py`
- MES 端設定檔：`c:\win_share\mes\mes_drill_receiver_config.json`
- MES 端 log：`D:\drill_export\receiver.log`
- 部署文件（MES 端視角）：`c:\win_share\mes\docs\drill_receiver_setup.md`

---

附錄：本文件命名為 `drill_push_dev_spec.md`，可以隨身碟拷貝到鑽孔開發機。請勿將 token 寫入此文件。

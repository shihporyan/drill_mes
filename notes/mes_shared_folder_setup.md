# MES 主機設定共用資料夾 — 交辦文件

**對象**：MES 主機系統管理員
**目的**：建立一個 Windows 共用資料夾，讓鑽孔監控運算電腦每天把稼動率資料 (CSV) 推過來

---

## 環境資訊

| 項目 | 值 |
|---|---|
| MES 主機 IP | `192.168.2.211` |
| 鑽孔運算電腦 IP | `192.168.2.50` |
| 兩台是否同網段 | ✅ 同 `192.168.2.0/24` |
| 作業系統 | Windows |
| 網路類型 | 內網（兩台都無 internet） |

---

## 要建立的東西總覽

| 項目 | 值（建議）|
|---|---|
| 共用資料夾路徑 | `D:\drill_export\`（若無 D 槽改 `C:\drill_export\`）|
| 共用名稱 (Share name) | `drill_export` |
| UNC 路徑 | `\\192.168.2.211\drill_export` |
| 寫入帳號 | `drill_writer`（本機帳號，非網域）|
| 帳號密碼 | 自訂強密碼，**勾「密碼永久有效」**|
| 寫入帳號權限 | 該資料夾 Modify（讀寫刪檔）|

---

## Step 1：建立資料夾

開啟「檔案總管」，到 `D:\` （或 `C:\`），新建資料夾 `drill_export`。

或用 PowerShell（**以系統管理員身分**）：
```powershell
New-Item -Path "D:\drill_export" -ItemType Directory
```

---

## Step 2：建立本機帳號 `drill_writer`

### 方法 A：UI（電腦管理）
1. 開始 → 右鍵「本機」/「我的電腦」 → 管理 → 「本機使用者和群組」 → 「使用者」
2. 右鍵 → 新使用者
3. 填寫：
   - 使用者名稱：`drill_writer`
   - 密碼：自訂強密碼（**請記下**，待會鑽孔端要用）
   - **取消勾「使用者必須在下次登入時變更密碼」**
   - **勾「密碼永久有效」** ⚠️ 這個一定要勾，不然 90 天後會過期推不過來
   - **勾「使用者無法變更密碼」**
4. 建立

### 方法 B：PowerShell（**以系統管理員身分**）
```powershell
$password = Read-Host -AsSecureString "輸入 drill_writer 的密碼"
New-LocalUser -Name "drill_writer" -Password $password -PasswordNeverExpires -UserMayNotChangePassword -Description "鑽孔監控資料推送專用"
```

> ⚠️ Windows Home 版沒有 `New-LocalUser`，需用方法 A。

---

## Step 3：設定 NTFS 權限（資料夾本身的存取權）

1. 右鍵 `D:\drill_export` → 內容 → 安全性 頁籤 → 編輯 → 新增
2. 輸入 `drill_writer` → 檢查名稱 → 確定
3. 給予權限：勾「修改 (Modify)」（會自動連帶勾讀取/寫入）→ 確定
4. **不要刪掉原本的 SYSTEM / Administrators 權限**

或用 PowerShell：
```powershell
$acl = Get-Acl "D:\drill_export"
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule("drill_writer","Modify","ContainerInherit,ObjectInherit","None","Allow")
$acl.AddAccessRule($rule)
Set-Acl "D:\drill_export" $acl
```

---

## Step 4：設定共用 (Share)

### 方法 A：UI
1. 右鍵 `D:\drill_export` → 內容 → 共用 頁籤 → 進階共用 → 勾「共用此資料夾」
2. 共用名稱填：`drill_export`
3. 點「權限」 → 移除 Everyone（如果有）→ 新增 `drill_writer` → 勾「變更 (Change)」+「讀取 (Read)」 → 確定
4. 確定 → 關閉

### 方法 B：PowerShell（**以系統管理員身分**）
```powershell
New-SmbShare -Name "drill_export" -Path "D:\drill_export" -ChangeAccess "drill_writer"
```

---

## Step 5：防火牆 — 開啟 SMB 給鑽孔運算電腦

同網段通常已通，但保險起見：

```powershell
# 確認 SMB-In 規則啟用（File and Printer Sharing）
Get-NetFirewallRule -DisplayGroup "檔案及印表機共用" | Where-Object {$_.Direction -eq "Inbound"} | Select-Object DisplayName, Enabled
```

如果有任何 `Enabled = False`，啟用：
```powershell
Enable-NetFirewallRule -DisplayGroup "檔案及印表機共用"
```

> 進階：若想限定**只有鑽孔電腦能連**，可以加白名單：
> ```powershell
> New-NetFirewallRule -DisplayName "Allow Drill Monitor SMB" -Direction Inbound -Protocol TCP -LocalPort 445 -RemoteAddress 192.168.2.50 -Action Allow
> ```
> 但要先確認其他需求（其他電腦是否也要連這台 MES）。

---

## Step 6：MES 端自我測試

在 MES 主機本機 PowerShell 執行：

```powershell
# 1. 共用是否建立
Get-SmbShare -Name "drill_export"

# 2. 帳號是否建立
Get-LocalUser -Name "drill_writer"

# 3. 從 MES 自己連自己（用 drill_writer 身份）
net use Z: \\192.168.2.211\drill_export /user:drill_writer
# 輸入剛才設的密碼，看是否能連上
dir Z:
net use Z: /delete
```

三個都成功 → MES 端設定完成。

---

## Step 7：請鑽孔運算電腦端測試（這步要對方執行）

把以下三行給鑽孔端在 cmd 跑：
```bat
ping 192.168.2.211
net view \\192.168.2.211
net use \\192.168.2.211\drill_export /user:drill_writer <密碼>
```

第三行成功（顯示「命令已順利完成」）→ 整條通路 OK。

---

## 驗收 Checklist

- [ ] `D:\drill_export\` 資料夾已建立
- [ ] `drill_writer` 帳號已建立，**密碼永久有效**已勾
- [ ] NTFS 權限：`drill_writer` 有 Modify
- [ ] Share 權限：`drill_writer` 有 Change
- [ ] `Get-SmbShare drill_export` 可查到
- [ ] MES 本機 `net use Z: \\192.168.2.211\drill_export /user:drill_writer` 成功
- [ ] 鑽孔端 `net view \\192.168.2.211` 看得到 drill_export 共用
- [ ] 鑽孔端 `net use \\192.168.2.211\drill_export /user:drill_writer` 成功
- [ ] 把 `drill_writer` 的密碼用安全方式交給鑽孔端設定者

---

## 給鑽孔端的交付資訊（完成後請填）

| 項目 | 值 |
|---|---|
| UNC 路徑 | `\\192.168.2.211\drill_export` |
| 帳號 | `drill_writer` |
| 密碼 | （另外用安全管道傳，**不要寫在這個檔案**）|
| 設定人員 | __________ |
| 設定日期 | __________ |

---

## 常見問題

**Q1：MES 主機重啟，共用會消失嗎？**
不會。共用設定存在登錄檔，重啟自動恢復。`drill_writer` 帳號、權限、防火牆規則也都是 persistent。

**Q2：可以改帳號名稱或路徑嗎？**
可以。改完後請更新 Step 7 的「給鑽孔端的交付資訊」表格，並通知鑽孔端設定者。

**Q3：鑽孔端密碼忘記怎麼辦？**
在 MES 主機重設 `drill_writer` 密碼即可（電腦管理 → 本機使用者 → 右鍵 drill_writer → 設定密碼）。重設後通知鑽孔端更新即可，**共用設定本身不需要重做**。

**Q4：之後想看鑽孔每天有沒有推檔，怎麼看？**
直接打開 `D:\drill_export\`，會看到類似 `drill_export_2026-05-12.csv` 的檔案，按日期累積。

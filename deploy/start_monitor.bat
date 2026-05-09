@echo off
REM =====================================================
REM  start_monitor.bat
REM
REM  Auto-launched at user login by HKCU Run key:
REM    HKCU\Software\Microsoft\Windows\CurrentVersion\Run
REM    DrillMonitor = C:\DrillMonitor\start_monitor.bat
REM
REM  Window title "DrillMonitor" lets FRESH_DEPLOY.bat
REM  taskkill cleanly before redeploying.
REM =====================================================

title DrillMonitor
cd /d C:\DrillMonitor

REM --- Grace period: let network stack / drivers come up after boot ---
echo [%date% %time%] DrillMonitor launching in 180s...
timeout /t 180 /nobreak

REM --- Re-establish SMB mappings for all 18 Takeuchi machines ---
REM  /persistent:yes alone is not enough: after power loss the previous
REM  session can be left in a stale "Disconnected" state with a bad
REM  cached NTLM token, and a plain re-issue becomes a no-op (Windows
REM  reports "the local device name is already in use"). Force-delete
REM  first so the re-mount establishes a fresh session.
echo [%date% %time%] Clearing stale SMB mappings...
for %%i in (11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28) do (
    net use \\10.10.1.%%i\LOG       /delete /yes >nul 2>&1
    net use \\10.10.1.%%i\NcProgram /delete /yes >nul 2>&1
    net use \\10.10.1.%%i           /delete /yes >nul 2>&1
)
net use \\10.10.1.31\LOG /delete /yes >nul 2>&1
for %%i in (32 33 34) do (
    net use \\10.10.1.%%i\LOG  /delete /yes >nul 2>&1
    net use \\10.10.1.%%i\INFO /delete /yes >nul 2>&1
)

echo [%date% %time%] Re-mounting SMB shares...
REM Each Takeuchi gets two persistent mappings: LOG (for log_collector
REM robocopy) and NcProgram (for o100_observer Phase 3 board routing).
for %%i in (11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28) do (
    net use \\10.10.1.%%i\LOG       "" /user:Takeuchi /persistent:yes >nul 2>&1
    net use \\10.10.1.%%i\NcProgram "" /user:Takeuchi /persistent:yes >nul 2>&1
)
REM --- Kataoka laser machines. L1 has LOG only (no INFO yet);
REM     L2-L4 have both LOG and INFO. ---
net use \\10.10.1.31\LOG "" /user:Guest /persistent:yes >nul 2>&1
for %%i in (32 33 34) do (
    net use \\10.10.1.%%i\LOG "" /user:Guest /persistent:yes >nul 2>&1
    net use \\10.10.1.%%i\INFO "" /user:Guest /persistent:yes >nul 2>&1
)

REM --- Small settle before health check ---
timeout /t 5 /nobreak >nul

REM --- Verify each share is actually reachable after re-mount.
REM     Output goes to console AND C:\DrillMonitor\smb_boot_check.log
REM     so an operator can scroll back / inspect after main.py takes over. ---
echo.
echo [%date% %time%] Running SMB boot health check...
python -m collector.health_check > C:\DrillMonitor\smb_boot_check.log 2>&1
type C:\DrillMonitor\smb_boot_check.log
findstr /C:"OFFLINE" C:\DrillMonitor\smb_boot_check.log >nul
if not errorlevel 1 (
    echo.
    echo *** WARNING: one or more machines are OFFLINE at boot ***
    echo *** See C:\DrillMonitor\smb_boot_check.log for details ***
    echo *** Parser will retry per cycle, but check control PCs  ***
    echo.
    timeout /t 10 /nobreak >nul
)

REM --- Run the monitor (keeps log visible in this window) ---
python main.py

echo.
echo *** main.py exited. Press any key to close. ***
pause >nul

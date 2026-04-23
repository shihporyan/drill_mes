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
REM  /persistent:yes should survive reboot via Credential Manager;
REM  re-issuing here is a belt-and-suspenders defense.
for %%i in (11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28) do (
    net use \\10.10.1.%%i\LOG "" /user:Takeuchi /persistent:yes >nul 2>&1
)

REM --- Small settle before robocopy fires its first cycle ---
timeout /t 5 /nobreak >nul

REM --- Run the monitor (keeps log visible in this window) ---
python main.py

echo.
echo *** main.py exited. Press any key to close. ***
pause >nul

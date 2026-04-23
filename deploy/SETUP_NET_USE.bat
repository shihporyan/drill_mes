@echo off
REM =====================================================
REM  SETUP_NET_USE.bat  (one-time setup)
REM
REM  Establishes persistent SMB mappings to all 18 Takeuchi
REM  drill machines. Run ONCE, as the user account that will
REM  own the Drill Monitor process.
REM
REM  /persistent:yes ensures Windows remembers these mappings
REM  in the user's credential store and reconnects automatically
REM  after reboot / power loss.
REM
REM  NOTE: "net use" is per-user. If Drill Monitor runs under a
REM  different user account (e.g. a scheduled task), run this
REM  batch as that user.
REM
REM  Re-run safe: existing mappings are deleted first.
REM =====================================================

echo.
echo === Setting up persistent SMB mappings (Takeuchi M01-M18) ===
echo.

REM --- Clean any stale mappings first ---
echo [Step 1] Removing stale mappings (ignore errors)...
for %%i in (11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28) do (
    net use \\10.10.1.%%i /delete >nul 2>&1
)

echo.
echo [Step 2] Creating persistent mappings...
echo.

REM --- 18 Takeuchi machines, /persistent:yes ---
for %%i in (11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28) do (
    echo   Mapping \\10.10.1.%%i\LOG ...
    net use \\10.10.1.%%i\LOG "" /user:Takeuchi /persistent:yes
    if errorlevel 1 (
        echo     WARNING: failed to map \\10.10.1.%%i\LOG
    )
)

echo.
echo [Step 3] Verifying current mappings:
echo.
net use

echo.
echo =====================================================
echo  Setup complete.
echo.
echo  Test: run "dir \\10.10.1.23\LOG" and confirm you
echo  can see the ##Drive.Log / ##TX1.Log files.
echo =====================================================
pause

@echo off
REM =====================================================
REM  Drill Monitor - Fresh Deploy Script
REM  Run on the Windows compute PC.
REM  Prereq: Python 3.8+ installed and in PATH.
REM =====================================================

REM Change to the directory where this script lives.
REM Without this, "python db\init_db.py" would resolve against
REM whatever cwd the user launched us from (often System32).
cd /d "%~dp0"

echo.
echo === Drill Monitor Fresh Deploy ===
echo Working dir: %CD%
echo.

REM --- Step 1: verify Python ---
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found in PATH
    echo Install Python 3.8+ and add to PATH
    pause
    exit /b 1
)
python --version

REM --- Step 2: stop running processes ---
echo.
echo [Step 2] Stopping any running drill_monitor processes...
taskkill /F /FI "WINDOWTITLE eq DrillMonitor" >nul 2>&1
REM Give processes time to close
timeout /t 2 /nobreak >nul

REM --- Step 3: remove old DB and logs ---
echo.
echo [Step 3] Removing old database and log files...
if exist drill_monitor.db (
    del /F drill_monitor.db
    echo   Deleted: drill_monitor.db
)
if exist drill_monitor.db-wal (
    del /F drill_monitor.db-wal
    echo   Deleted: drill_monitor.db-wal
)
if exist drill_monitor.db-shm (
    del /F drill_monitor.db-shm
    echo   Deleted: drill_monitor.db-shm
)
if exist drill_monitor.log (
    del /F drill_monitor.log
    echo   Deleted: drill_monitor.log
)
if exist drill_monitor.log.1 (
    del /F drill_monitor.log.*
    echo   Deleted: drill_monitor.log backups
)

REM --- Step 4: init fresh DB ---
echo.
echo [Step 4] Initializing fresh database...
python db\init_db.py

REM --- Step 5: ensure backup_root exists ---
echo.
echo [Step 5] Ensuring backup directory exists...
if not exist "C:\DrillLogs" (
    mkdir "C:\DrillLogs"
    echo   Created: C:\DrillLogs
) else (
    echo   OK: C:\DrillLogs exists
)

REM --- Step 6: run parser tests ---
echo.
echo [Step 6] Running parser tests...
python -m unittest tests.test_parser_accuracy -v

REM --- Step 7: machine connectivity check ---
echo.
echo [Step 7] Checking machine connectivity...
python collector\health_check.py

echo.
echo =====================================================
echo  Fresh deploy complete!
echo.
echo  Next steps:
echo    1. Edit config\settings.json:
echo       - Set http_host to this PC's office network IP
echo    2. Edit config\machines.json:
echo       - Set enabled=true for machines to monitor
echo    3. Start the system:
echo       python main.py
echo.
echo  Dashboard will be at http://[http_host]:8080
echo =====================================================
pause

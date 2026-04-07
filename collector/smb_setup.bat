@echo off
REM === SMB Connection Setup for Drill Monitoring ===
REM Run this once after all network cables are installed.
REM Establishes persistent SMB connections to each machine's LOG share.

net use \\10.10.1.11\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.12\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.13\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.14\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.15\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.16\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.17\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.18\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.19\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.20\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.21\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.22\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.23\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.24\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.25\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.26\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.27\LOG /user:Takeuchi "" /persistent:yes 2>nul
net use \\10.10.1.28\LOG /user:Takeuchi "" /persistent:yes 2>nul

echo.
echo Done. Verify connections:
net use
pause

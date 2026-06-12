@echo off
setlocal
cd /d "%~dp0"
title OSRS Dashboard

REM If it's already running, just open the dashboard in your browser.
netstat -ano | findstr /R /C:":8791 .*LISTENING" >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  echo Dashboard already running - opening browser...
  start "" "http://127.0.0.1:8791"
  exit /b 0
)

REM Start the server in a minimized window (keep that window open while you use it).
echo Starting OSRS Dashboard...
start "OSRS Dashboard" /min cmd /c "python server.py --host 127.0.0.1 --port 8791 || py server.py --host 127.0.0.1 --port 8791"

REM Give it a couple seconds to boot, then open the dashboard.
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:8791"
exit /b 0

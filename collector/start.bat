@echo off
setlocal
cd /d "%~dp0"
if /i "%~1"=="--inside" goto inside
"%ComSpec%" /d /k call "%~f0" --inside
exit /b
:inside
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Python environment missing. Run setup.bat first.
  exit /b 1
)
".venv\Scripts\python.exe" -c "import playwright.sync_api, requests; from zoneinfo import ZoneInfo; ZoneInfo('Asia/Shanghai')"
if errorlevel 1 (
  echo [ERROR] Dependencies incomplete. Run setup.bat again.
  exit /b 1
)
echo [MX] Starting local collector. Login in the browser it opens.
echo [MX] Keep this window open. Ctrl+C stops collection.
".venv\Scripts\python.exe" -X utf8 -u local_collect.py
set "MX_EXIT=%ERRORLEVEL%"
echo.
echo [MX] Collector stopped. Exit code: %MX_EXIT%
echo [MX] Read any error above. This window stays open; type exit to close.
exit /b %MX_EXIT%


@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
py -3 --version >nul 2>&1
if errorlevel 1 (
  python --version >nul 2>&1
  if errorlevel 1 goto no_python
  set "PY=python"
) else (
  set "PY=py -3"
)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 goto no_python
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -c "from zoneinfo import ZoneInfo; print('Timezone OK:', ZoneInfo('Asia/Shanghai'))"
if errorlevel 1 goto failed
echo Setup complete. Double-click start.bat to run.
echo No token was requested or saved during setup.
pause
exit /b 0
:no_python
echo Install Python 3.10 or newer from python.org, then run setup.bat again.
echo Select 'Add python.exe to PATH' during installation.
pause
exit /b 1
:failed
echo Setup failed. Check the message above, then retry setup.bat.
pause
exit /b 1
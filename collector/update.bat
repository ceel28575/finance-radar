@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo [FinanceRadar] 正在安全更新本机采集器……
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1"
if errorlevel 1 (
  echo.
  echo [FinanceRadar] 更新未完成。请保留此窗口中的错误信息。
  pause
  exit /b 1
)
echo.
echo [FinanceRadar] 更新脚本已完成。新版采集器会在独立窗口启动。
pause
exit /b 0

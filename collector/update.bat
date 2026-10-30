@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo [FinanceRadar] 正在下载最新安全更新程序……
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
 "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $p=Join-Path $env:TEMP ('finance-radar-updater-'+[guid]::NewGuid().ToString('N')+'.ps1'); try { Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/ceel28575/finance-radar/main/collector/update.ps1' -OutFile $p -UseBasicParsing; & $p -TargetCollectorRoot '%~dp0'; if ($LASTEXITCODE -ne $null) { exit $LASTEXITCODE } } catch { Write-Host $_.Exception.Message -ForegroundColor Red; exit 1 } finally { if (Test-Path $p) { Remove-Item $p -Force -ErrorAction SilentlyContinue } }"
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

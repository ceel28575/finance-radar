param([string]$TargetCollectorRoot = $PSScriptRoot)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$collectorRoot = [IO.Path]::GetFullPath($TargetCollectorRoot)
$repoRoot = Split-Path -Parent $collectorRoot
$repo = 'ceel28575/finance-radar'
$archiveUrl = 'https://github.com/ceel28575/finance-radar/archive/refs/heads/main.zip'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupRoot = Join-Path $env:LOCALAPPDATA ("FinanceRadar\backups\" + $stamp)
$tempRoot = Join-Path $env:TEMP ("finance-radar-update-" + [guid]::NewGuid().ToString('N'))
$zipPath = Join-Path $tempRoot 'main.zip'
$extractRoot = Join-Path $tempRoot 'extract'

function Write-Step([string]$Message) {
    Write-Host ("[FinanceRadar] " + $Message) -ForegroundColor Cyan
}

function Copy-ProjectFiles([string]$Source, [string]$Destination) {
    $skipDirs = @('.git', '.venv', 'node_modules', 'mx_profile', 'exports', '__pycache__', '.next', '.wrangler', 'backups', 'dist', 'build')
    $skipFiles = @('mx_outbox.db', 'mx_cloud.db', 'cloud-config.json', 'config.json', 'collector.lock')
    Get-ChildItem -LiteralPath $Source -File -Recurse -Force | ForEach-Object {
        $relative = $_.FullName.Substring($Source.Length).TrimStart('\', '/')
        $parts = $relative -split '[\\/]'
        $fileName = [IO.Path]::GetFileName($relative)
        if (@($parts | Where-Object { $skipDirs -contains $_ }).Count -gt 0) { return }
        if ($skipFiles -contains $fileName) { return }
        if ($fileName -match '^\.env($|\.)') { return }
        $target = Join-Path $Destination $relative
        $targetDir = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $targetDir)) {
            New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
        }
        Copy-Item -LiteralPath $_.FullName -Destination $target -Force
    }
}

function Backup-LocalState([string]$RelativePath) {
    $source = Join-Path $repoRoot $RelativePath
    if (-not (Test-Path -LiteralPath $source)) { return }
    $destination = Join-Path $backupRoot ("local-state\" + $RelativePath)
    $parent = Split-Path -Parent $destination
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Copy-Item -LiteralPath $source -Destination $destination -Recurse -Force
}

try {
    Write-Step '检查采集器是否已经退出……'
    try {
        $pythonProcesses = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
            Where-Object { $_.CommandLine -and $_.CommandLine -match 'local_collect\.py' })
        if ($pythonProcesses.Count -gt 0) {
            throw '检测到 MX 采集器仍在运行。请回到采集窗口按 Ctrl+C，等它停止后再双击 update.bat。为保护数据库，本程序不会强制结束采集器。'
        }
        $profileProcesses = @(Get-CimInstance Win32_Process |
            Where-Object { $_.CommandLine -and $_.CommandLine -match 'mx_profile' -and $_.Name -match 'msedge|chrome|chromium' })
        if ($profileProcesses.Count -gt 0) {
            throw '检测到仍有浏览器占用 MX 登录档案。请关闭采集器打开的 MX 浏览器，再运行 update.bat。'
        }
    } catch {
        if ($_.Exception.Message -like '检测到*') { throw }
        Write-Warning '无法完整读取进程列表；继续前请确认采集器和它打开的浏览器均已关闭。'
    }

    if (-not (Test-Path -LiteralPath (Join-Path $collectorRoot 'start.bat'))) {
        throw '没有找到 collector\start.bat。请把 update.bat 和 update.ps1 放在 collector 文件夹中运行。'
    }

    Write-Step '下载 GitHub 最新稳定源码……'
    New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
    Invoke-WebRequest -Uri $archiveUrl -OutFile $zipPath -UseBasicParsing
    New-Item -ItemType Directory -Path $extractRoot -Force | Out-Null
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractRoot -Force
    $sourceRoot = Get-ChildItem -LiteralPath $extractRoot -Directory | Select-Object -First 1
    if (-not $sourceRoot) { throw 'GitHub 压缩包为空，更新已取消。' }
    if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot.FullName 'collector\local_collect.py')) -or
        -not (Test-Path -LiteralPath (Join-Path $sourceRoot.FullName 'dashboard.html'))) {
        throw '下载内容结构校验失败，未修改本机文件。'
    }

    Write-Step '备份 MX 登录档案、数据库和同步配置……'
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    $statePaths = @(
        'collector\mx_profile',
        'collector\mx_outbox.db',
        'collector\mx_cloud.db',
        'collector\cloud-config.json',
        'collector\config.json',
        'collector\exports',
        'collector\logs'
    )
    foreach ($path in $statePaths) { Backup-LocalState $path }
    Get-ChildItem -LiteralPath $collectorRoot -File -Filter '*.log' -ErrorAction SilentlyContinue |
        ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $backupRoot $_.Name) -Force }

    Write-Step '备份当前程序文件……'
    $codeBackup = Join-Path $backupRoot 'source'
    New-Item -ItemType Directory -Path $codeBackup -Force | Out-Null
    Copy-ProjectFiles $repoRoot $codeBackup

    Write-Step '更新程序文件（保留本地登录、数据库、密钥配置和虚拟环境）……'
    Copy-ProjectFiles $sourceRoot.FullName $repoRoot

    $python = Join-Path $collectorRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        throw '没有找到 collector\.venv。新版文件已复制；请先运行 setup.bat 安装环境，再运行 start.bat。'
    }

    Write-Step '检查 Python 依赖……'
    & $python -m pip install -r (Join-Path $collectorRoot 'requirements.txt') --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { throw 'Python 依赖更新失败。旧数据和更新前代码备份均已保留。' }

    Write-Step '检查 Python 语法并运行本机测试……'
    & $python -m compileall -q $collectorRoot
    if ($LASTEXITCODE -ne 0) { throw 'Python 语法检查失败。' }
    & $python -m unittest discover -s (Join-Path $repoRoot 'tests') -p 'test_*.py' -v
    if ($LASTEXITCODE -ne 0) { throw '本机 Python 测试未通过。' }

    Write-Host ''
    Write-Host '更新完成。' -ForegroundColor Green
    Write-Host ("本机数据备份位置：" + $backupRoot)
    Write-Host 'MX 登录档案、历史数据库、同步配置和 Ollama 模型均未删除。'
    Write-Step '启动新版采集器……'
    Start-Process -FilePath (Join-Path $collectorRoot 'start.bat') -WorkingDirectory $collectorRoot
    Write-Host '新版采集器已打开独立窗口。请在新窗口按提示输入豆包 API Key（如需），并确认 MX 页面已登录。' -ForegroundColor Green
}
catch {
    Write-Host ''
    Write-Host ("更新未能完整完成：" + $_.Exception.Message) -ForegroundColor Red
    if (Test-Path -LiteralPath (Join-Path $backupRoot 'source')) {
        Write-Warning '正在从备份恢复更新前的程序文件……'
        try { Copy-ProjectFiles (Join-Path $backupRoot 'source') $repoRoot; Write-Host '程序文件已恢复；本机数据库和登录档案未改动。' -ForegroundColor Yellow }
        catch { Write-Warning '自动恢复未完成，请保留备份目录并联系支持。' }
    }
    Write-Host ("备份目录：" + $backupRoot)
    exit 1
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# start-panwatch.ps1
# Windows 一键启动 PanWatch 前后端（无需 Git Bash）
# 用法：双击 start-panwatch.bat，或在 PowerShell 中
#   powershell -NoProfile -ExecutionPolicy Bypass -File start-panwatch.ps1
$ErrorActionPreference = 'Stop'
$ROOT   = $PSScriptRoot
$logDir = Join-Path $ROOT '.logs'
$backendPort = 8000
$frontendPort = 5183

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

function Write-Log($msg) { Write-Host "[PanWatch] $msg" }

function Test-PortListening($port) {
    try {
        $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        if ($c) { return $true }
    } catch {}
    $lines = netstat -ano 2>$null | Select-String ":$port\s"
    foreach ($l in $lines) { if ($l -match 'LISTENING') { return $true } }
    return $false
}

# 端口占用检查（vite strictPort，被占会直接报错，故先查）
if (Test-PortListening $backendPort) {
    Write-Log "后端端口 $backendPort 已被占用，请先运行 stop-panwatch.bat"
    exit 1
}
if (Test-PortListening $frontendPort) {
    Write-Log "前端端口 $frontendPort 已被占用，请先运行 stop-panwatch.bat"
    exit 1
}

# 后端启动脚本（优先 pnpm，否则直接 node 调 vite 入口）
$beBat = Join-Path $logDir 'run-backend.bat'
$beLog = Join-Path $logDir 'backend.log'
@"
@echo off
set DEV_RELOAD=1
cd /d "$ROOT"
call .venv\Scripts\activate.bat 2>nul
.venv\Scripts\python.exe server.py >> "$beLog" 2>&1
"@ | Set-Content -Path $beBat -Encoding ASCII

$feBat = Join-Path $logDir 'run-frontend.bat'
$feLog = Join-Path $logDir 'frontend.log'
@"
@echo off
cd /d "$ROOT\frontend"
where pnpm >nul 2>&1
if %errorlevel%==0 (
  pnpm dev >> "$feLog" 2>&1
) else (
  node "$ROOT\frontend\node_modules\vite\bin\vite.js" >> "$feLog" 2>&1
)
"@ | Set-Content -Path $feBat -Encoding ASCII

# 拉起（隐藏窗口，记录 PID）
$be = Start-Process -FilePath $beBat -PassThru -WindowStyle Hidden
$fe = Start-Process -FilePath $feBat -PassThru -WindowStyle Hidden
$be.Id | Set-Content (Join-Path $logDir 'backend.pid')
$fe.Id | Set-Content (Join-Path $logDir 'frontend.pid')
Write-Log "已启动 -> 后端 PID=$($be.Id), 前端 PID=$($fe.Id)"

# 轮询后端 health
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$backendPort/api/health" -TimeoutSec 2 -ErrorAction SilentlyContinue
        if ($r.status -eq 'ok') { $ready = $true; break }
    } catch {}
    Start-Sleep -Seconds 2
}
if ($ready) {
    Write-Log "后端已就绪: http://127.0.0.1:$backendPort/api/health"
} else {
    Write-Log "后端未在预期时间内就绪，请查看日志: $beLog"
}
Write-Log "前端地址: http://127.0.0.1:$frontendPort  (日志: $feLog)"
Write-Log "完成。停止请运行 stop-panwatch.bat"

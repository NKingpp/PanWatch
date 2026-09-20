# stop-panwatch.ps1
# Windows 一键停止 PanWatch 前后端（无需 Git Bash）
$ErrorActionPreference = 'Stop'
$ROOT   = $PSScriptRoot
$logDir = Join-Path $ROOT '.logs'
$backendPort = 8000
$frontendPort = 5183

function Write-Log($msg) { Write-Host "[PanWatch] $msg" }

function Stop-PidFile($name) {
    $pf = Join-Path $logDir "$name.pid"
    if (Test-Path $pf) {
        $pidv = (Get-Content $pf -ErrorAction SilentlyContinue).Trim()
        if ($pidv -match '^\d+$') {
            & taskkill.exe /PID $pidv /T /F 2>$null | Out-Null
            Write-Log "已停止 $name (PID $pidv, 含子进程树)"
        }
        Remove-Item $pf -Force -ErrorAction SilentlyContinue
    } else {
        Write-Log "未找到 $name 的 PID 文件，跳过"
    }
}

Stop-PidFile 'backend'
Stop-PidFile 'frontend'

# 兜底：强杀仍在 LISTENING 8000 / 5183 的进程树
foreach ($port in @($backendPort, $frontendPort)) {
    $lines = netstat -ano 2>$null | Select-String ":$port\s"
    foreach ($l in $lines) {
        if ($l -match 'LISTENING\s+(\d+)') {
            $p = $matches[1]
            & taskkill.exe /PID $p /T /F 2>$null | Out-Null
            Write-Log "兜底强杀端口 $port (PID $p)"
        }
    }
}

Start-Sleep -Seconds 1
$any = $false
foreach ($port in @($backendPort, $frontendPort)) {
    $ls = netstat -ano 2>$null | Select-String ":$port\s" | Where-Object { $_ -match 'LISTENING' }
    if ($ls) { $any = $true }
}
if ($any) {
    Write-Log "仍有进程监听 8000/5183，请手动检查。"
} else {
    Write-Log "端口已释放，PanWatch 已停止。"
}


<#
.SYNOPSIS
    Stop MY-SECRETARY background process.
    Called from stop_secretary.bat. Can also be run directly.
#>

param(
    [string]$BaseDir = $PSScriptRoot
)

$pidFile = Join-Path $BaseDir 'logs\secretary.pid'

if (-not (Test-Path $pidFile)) {
    Write-Host 'MY-SECRETARY is not running.'
    exit 0
}

$savedPid = (Get-Content $pidFile -Raw).Trim()

if ([string]::IsNullOrEmpty($savedPid)) {
    Write-Host 'PID file is empty. Removing.'
    Remove-Item $pidFile -Force
    exit 0
}

$proc = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
if (-not $proc -or $proc.HasExited) {
    Write-Host 'MY-SECRETARY is already stopped.'
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    exit 0
}

Write-Host "Stopping MY-SECRETARY (PID: $savedPid)..."
taskkill /F /PID $savedPid /T 2>&1 | Out-Null

Start-Sleep 1
$check = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
if (-not $check -or $check.HasExited) {
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    Write-Host 'MY-SECRETARY stopped.'
} else {
    Write-Host "Failed to stop. Run manually: taskkill /F /PID $savedPid /T"
    exit 1
}

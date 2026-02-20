
<#
.SYNOPSIS
    Start MY-SECRETARY as a hidden background process.
    Called from start_secretary.bat. Can also be run directly.
#>

param(
    [string]$BaseDir = $PSScriptRoot
)

$logDir  = Join-Path $BaseDir 'logs'
$logFile = Join-Path $logDir  'secretary.log'
$pidFile = Join-Path $logDir  'secretary.pid'
$srcDir  = Join-Path $BaseDir 'src'

# Create logs directory if needed
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

# Prevent duplicate launch
if (Test-Path $pidFile) {
    $oldPid = (Get-Content $pidFile -Raw).Trim()
    $proc = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
    if ($proc -and -not $proc.HasExited) {
        Write-Host "MY-SECRETARY is already running (PID: $oldPid)"
        exit 0
    }
    Remove-Item $pidFile -Force
}

# Start hidden cmd.exe that runs Python and appends stdout+stderr to secretary.log
$q       = [char]34
$cmdArgs = '/c python main.py >> ' + $q + $logFile + $q + ' 2>&1'
$p       = Start-Process cmd.exe -ArgumentList $cmdArgs -WorkingDirectory $srcDir -WindowStyle Hidden -PassThru

$p.Id | Set-Content $pidFile -Encoding ascii -NoNewline

Write-Host "MY-SECRETARY started in background (PID: $($p.Id))"
Write-Host "Log: $logFile"

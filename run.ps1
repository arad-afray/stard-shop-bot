# اجرای ربات با Restart خودکار (ویندوز / PowerShell):   powershell -ExecutionPolicy Bypass -File .\run.ps1
#
# - کد خروج 0: توقف عادی (Ctrl+C)  → اسکریپت تمام می‌شود
# - کد خروج 3: درخواست Restart (بعد از به‌روزرسانی یا بازگردانی پشتیبان) → فوراً دوباره اجرا
# - هر کد دیگر: کرش → Restart با فاصله‌ی افزایشی (5، 10، 20 … حداکثر 300 ثانیه)
# - بیش از 5 کرش در 10 دقیقه: 10 دقیقه صبر (جلوگیری از Restart Loop)
# - اگر نسخه‌ی تازه‌نصب‌شده 3 بار پشت سر هم در کمتر از 60 ثانیه کرش کند و data\update-pending.json
#   وجود داشته باشد: خودکار به commit قبلی برمی‌گردد (Rollback)
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
$env:STARD_SUPERVISED = "1"
$env:PYTHONUTF8 = "1"
$python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
New-Item -ItemType Directory -Force -Path "logs" | Out-Null
$log = Join-Path "logs" "supervisor.log"
function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding UTF8
}

$baseDelay = if ($env:STARD_RESTART_DELAY) { [int]$env:STARD_RESTART_DELAY } else { 5 }
$delay = $baseDelay
$crashes = @()
$fastCrashes = 0
while ($true) {
    Write-Log "starting bot"
    $started = Get-Date
    & $python -m bot
    $code = $LASTEXITCODE
    $ran = ((Get-Date) - $started).TotalSeconds
    if ($code -eq 0) { Write-Log "bot stopped normally"; break }
    if ($code -eq 3) { Write-Log "restart requested"; $delay = $baseDelay; $fastCrashes = 0; continue }

    Write-Log "bot crashed (exit $code after $([int]$ran)s)"
    if ($ran -lt 60) { $fastCrashes++ } else { $fastCrashes = 0; $delay = $baseDelay }
    $pending = Join-Path "data" "update-pending.json"
    if ($fastCrashes -ge 3 -and (Test-Path $pending)) {
        $info = Get-Content $pending -Raw | ConvertFrom-Json
        if ($info.from_commit) {
            Write-Log "new version keeps crashing - rolling back to $($info.from_commit)"
            & git checkout --force --detach $info.from_commit
            & $python -m pip install -q -r requirements.txt
            Remove-Item $pending -Force
            $fastCrashes = 0
            continue
        }
    }
    $now = Get-Date
    $crashes = @($crashes | Where-Object { ($now - $_).TotalSeconds -lt 600 }) + $now
    if ($crashes.Count -gt 5) {
        Write-Log "too many crashes ($($crashes.Count) in 10 min) - waiting 10 minutes"
        Start-Sleep -Seconds 600
        $crashes = @()
    } else {
        Write-Log "restarting in $delay s"
        Start-Sleep -Seconds $delay
        $delay = [Math]::Min($delay * 2, 300)
    }
}

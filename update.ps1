# به‌روزرسانی ربات روی ویندوز (از نسخه‌ی ۲ یا هر نسخه‌ی قبلی):
#     powershell -ExecutionPolicy Bypass -File .\update.ps1
# یا برای یک نسخه/شاخه‌ی مشخص:
#     powershell -ExecutionPolicy Bypass -File .\update.ps1 -Ref v3.0.0
#
# مراحل: پشتیبان از data و .env ← دریافت کد ← نصب کتابخانه‌ها ← مهاجرت پایگاه داده ← بررسی سلامت.
# اگر هر مرحله شکست بخورد، کد و پوشه‌ی data خودکار به حالت قبل برمی‌گردند.
# قبل از اجرا ربات را با Ctrl+C خاموش کنید.
param(
    [string]$Ref = "",
    [string]$Branch = "claude/starship-bot-github-access-qggts8"
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$env:PYTHONUTF8 = "1"
$python = if ($env:PYTHON) { $env:PYTHON } else { "python" }

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "`nERROR: $msg" -ForegroundColor Red }
function Run($exe, [string[]]$argv) {
    & $exe @argv
    if ($LASTEXITCODE -ne 0) { throw "$exe $($argv -join ' ') failed (exit $LASTEXITCODE)" }
}

if (-not (Test-Path ".git")) { Fail "This folder is not a git clone of stard-shop-bot."; exit 1 }
if (-not (Test-Path ".env")) { Fail ".env not found. Copy .env.example to .env and fill it first."; exit 1 }

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = "update-backup-$stamp"
$previous = (& git rev-parse HEAD).Trim()

Step "1/5 Backup data and .env -> $backup"
New-Item -ItemType Directory -Force -Path $backup | Out-Null
if (Test-Path "data") { Copy-Item "data" (Join-Path $backup "data") -Recurse }
Copy-Item ".env" (Join-Path $backup ".env")

try {
    Step "2/5 Download new version"
    Run "git" @("checkout", "--", ".")
    if ($Ref -eq "") {
        # آخرین Release رسمی (تگ vX.Y.Z)؛ اگر نبود، شاخه‌ی توسعه
        $tags = (& git ls-remote --tags --refs origin "v*") | ForEach-Object { ($_ -split "refs/tags/")[1] } |
            Where-Object { $_ -match '^v\d+\.\d+\.\d+$' } |
            Sort-Object { [version]($_.TrimStart("v")) } -Descending
        $Ref = if ($tags) { @($tags)[0] } else { $Branch }
    }
    Write-Host "target: $Ref"
    Run "git" @("fetch", "--force", "origin", "${Ref}:refs/remotes/origin/update-target")
    Run "git" @("checkout", "--force", "--detach", "origin/update-target")

    Step "3/5 Install libraries"
    Run $python @("-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", "requirements.txt")

    Step "4/5 Upgrade database (your data is kept)"
    Run $python @("-m", "bot.migrate")

    Step "5/5 Health check"
    Run $python @("-m", "bot.selfcheck")
}
catch {
    Fail "$_"
    Write-Host "Rolling back to $previous ..." -ForegroundColor Yellow
    & git checkout --force $previous | Out-Null
    if (Test-Path (Join-Path $backup "data")) {
        if (Test-Path "data") { Remove-Item "data" -Recurse -Force }
        Copy-Item (Join-Path $backup "data") "data" -Recurse
    }
    & $python -m pip install -q --disable-pip-version-check -r requirements.txt | Out-Null
    Write-Host "Rolled back. Nothing was lost. Backup: $backup" -ForegroundColor Yellow
    exit 1
}

$version = (& $python -c "import bot; print(bot.__version__)").Trim()
Write-Host "`nUpdated to v$version. Backup of your data: $backup" -ForegroundColor Green
Write-Host "Start the bot with auto-restart:  powershell -ExecutionPolicy Bypass -File .\run.ps1" -ForegroundColor Green

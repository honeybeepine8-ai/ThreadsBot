# ============================================================
# ThreadsBot Windows セットアップスクリプト (PowerShell)
# ============================================================
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
#
# Prerequisites:
#   - Python 3.11+ on PATH
# ============================================================

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Push-Location $ProjectRoot

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " ThreadsBot Setup" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ------------------------------------------------------------------
# 1. Python virtual environment
# ------------------------------------------------------------------
Write-Host "[1/4] Creating Python virtual environment (.venv) ..." -ForegroundColor Yellow

if (Test-Path ".venv") {
    Write-Host "  -> .venv already exists, skipping creation." -ForegroundColor Gray
} else {
    python -m venv .venv
    Write-Host "  -> .venv created." -ForegroundColor Green
}

# Activate
$ActivateScript = Join-Path ".venv" "Scripts" "Activate.ps1"
if (-Not (Test-Path $ActivateScript)) {
    Write-Host "[ERROR] Could not find $ActivateScript. Is Python installed?" -ForegroundColor Red
    Pop-Location
    exit 1
}
& $ActivateScript

# ------------------------------------------------------------------
# 2. Install dependencies
# ------------------------------------------------------------------
Write-Host "[2/4] Installing dependencies from requirements.txt ..." -ForegroundColor Yellow
pip install --upgrade pip | Out-Null
pip install -r requirements.txt
Write-Host "  -> Dependencies installed." -ForegroundColor Green

# ------------------------------------------------------------------
# 3. Copy .env.example -> .env (if .env does not exist)
# ------------------------------------------------------------------
Write-Host "[3/4] Checking .env file ..." -ForegroundColor Yellow

if (Test-Path ".env") {
    Write-Host "  -> .env already exists, skipping copy." -ForegroundColor Gray
} else {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Host "  -> .env.example copied to .env — edit it with your API keys." -ForegroundColor Green
    } else {
        Write-Host "  -> .env.example not found, skipping." -ForegroundColor Gray
    }
}

# ------------------------------------------------------------------
# 4. Create initial data files
# ------------------------------------------------------------------
Write-Host "[4/4] Initializing data files ..." -ForegroundColor Yellow
python scripts/init_data.py
Write-Host "  -> Data files initialized." -ForegroundColor Green

# ------------------------------------------------------------------
# Done
# ------------------------------------------------------------------
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Setup complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  1. Edit .env with your API keys"
Write-Host "  2. Activate venv:  .\.venv\Scripts\Activate.ps1"
Write-Host "  3. Run an agent:   python -m core.scheduler poster"
Write-Host "  4. Or start all:   python -m core.scheduler all"
Write-Host ""

Pop-Location

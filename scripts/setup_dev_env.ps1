# GoLogs developer environment setup for Windows PowerShell (PRD §17).
# Automates steps 1-5; steps 6-7 (seed data, run servers) are left as
# explicit commands the developer runs themselves afterward, so they see
# the ingestion/startup output directly rather than it being hidden here.
#
# Usage: powershell -ExecutionPolicy Bypass -File scripts\setup_dev_env.ps1

$ErrorActionPreference = "Stop"

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

Write-Host "== GoLogs dev environment setup ==" -ForegroundColor Cyan

# --- Step 1: verify prerequisites are installed ---
$missing = @()
if (-not (Test-CommandExists "python")) { $missing += "Python 3.12 (https://www.python.org/downloads/)" }
if (-not (Test-CommandExists "node"))   { $missing += "Node.js 20 LTS (https://nodejs.org/)" }
if (-not (Test-CommandExists "ollama")) { $missing += "Ollama for Windows (https://ollama.com/download)" }
if (-not (Test-CommandExists "git"))    { $missing += "Git (https://git-scm.com/download/win)" }

if ($missing.Count -gt 0) {
    Write-Host "Missing prerequisites -- install these first:" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "  - $_" }
    exit 1
}

$pyVersion = (python --version) -replace "Python ", ""
Write-Host "Found Python $pyVersion, Node $((node --version)), Ollama, Git." -ForegroundColor Green

# --- Step 2: pull the default local model ---
Write-Host "`nPulling llama3.1:8b-instruct-q4_K_M (this can take a while on first run)..." -ForegroundColor Cyan
ollama pull llama3.1:8b-instruct-q4_K_M

# --- Step 3: backend venv + editable install ---
Write-Host "`nSetting up backend virtual environment..." -ForegroundColor Cyan
Push-Location backend
python -m venv .venv
& .\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -e ".[dev]"
Pop-Location

# --- Step 4: frontend deps ---
Write-Host "`nInstalling frontend dependencies..." -ForegroundColor Cyan
Push-Location frontend
npm install
Pop-Location

# --- Step 5: .env ---
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "`nCreated .env from .env.example." -ForegroundColor Green
} else {
    Write-Host "`n.env already exists, leaving it untouched." -ForegroundColor Yellow
}

Write-Host "`n== Setup complete ==" -ForegroundColor Cyan
Write-Host "Next steps:"
Write-Host "  1. python scripts\seed_demo_case.py"
Write-Host "  2. cd backend; .venv\Scripts\Activate.ps1; uvicorn app.main:app --reload"
Write-Host "  3. (new terminal) cd frontend; npm run dev"

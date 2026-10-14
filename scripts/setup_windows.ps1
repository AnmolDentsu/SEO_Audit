$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
py -m venv backend\.venv
& backend\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".\backend[dev]"
python -m playwright install chromium
Set-Location backend
alembic upgrade head
Set-Location ..\frontend
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
npm install
Write-Host "Setup complete. Open three terminals and run API, worker, and frontend commands from README.md."

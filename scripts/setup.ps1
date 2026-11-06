# One-time setup on Windows. Run from the repository folder:
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 1. Python virtual environment + dependencies
if (-not (Test-Path ".venv")) { python -m venv .venv }
& .venv\Scripts\python.exe -m pip install --upgrade pip
& .venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. Config file (edit it afterwards)
if (-not (Test-Path "config.yaml")) { Copy-Item config.example.yaml config.yaml }

# 3. Claude Code project files: MCP server (using the venv's python) and tool permissions
$py = (Resolve-Path ".venv\Scripts\python.exe").Path -replace '\\', '/'
(Get-Content setup\claude\mcp.json -Raw) -replace '"command": "python"', ('"command": "' + $py + '"') |
    Set-Content .mcp.json -Encoding UTF8
New-Item -ItemType Directory -Force .claude | Out-Null
if (-not (Test-Path ".claude\settings.json")) { Copy-Item setup\claude\settings.json .claude\settings.json }

New-Item -ItemType Directory -Force logs, cache, data | Out-Null
Write-Host ""
Write-Host "Done. Next:"
Write-Host "  1) Edit config.yaml (watchlist, mt5.symbol_suffix, risk limits)."
Write-Host "  2) Set MT5_LOGIN / MT5_PASSWORD / MT5_SERVER as user environment variables (setx)."
Write-Host "  3) Open MT5 and log in to the DEMO account."
Write-Host "  4) Run 'claude' in this folder and type /trade-cycle to test one cycle."
Write-Host "  5) scripts\install_tasks.ps1 to schedule the routines."

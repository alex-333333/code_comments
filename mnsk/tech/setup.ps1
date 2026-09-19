# One-shot environment setup for Windows PowerShell.
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Optional     # also install spaCy + English model
#
# The venv Python is called directly (no Activate.ps1), so the script works even when
# PowerShell blocks running scripts (ExecutionPolicy). Messages are ASCII on purpose:
# Windows PowerShell 5.1 reads .ps1 files without a BOM in the ANSI codepage.

param([switch]$Optional)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "Python was not found in PATH. Install Python 3.10+ from https://www.python.org/downloads/ and tick 'Add python.exe to PATH'."
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "== Creating virtual environment .venv"
    python -m venv .venv
}
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

Write-Host "== Installing dependencies (pip can be slow on a weak connection; --timeout is raised)"
& $py -m pip install --upgrade pip --timeout 120 --retries 8
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
& $py -m pip install -r requirements.txt --timeout 180 --retries 8
if ($LASTEXITCODE -ne 0) { throw "pip install failed. Check the internet connection and re-run this script: finished downloads are cached." }

if ($Optional) {
    Write-Host "== Installing optional spaCy"
    & $py -m pip install -r requirements-optional.txt --timeout 180 --retries 8
    & $py -m spacy download en_core_web_sm
}

Write-Host "== Downloading tree-sitter grammars and checking the environment"
& $py mnsk.py setup
if ($LASTEXITCODE -ne 0) { throw "environment check failed, see messages above" }

Write-Host ""
Write-Host "Done. Try the demo:   .\.venv\Scripts\python.exe mnsk.py demo"
Write-Host "Docs:                 docs\02-quickstart.md"

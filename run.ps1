Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Test-Path -Path ".venv\Scripts\python.exe" -PathType Leaf)) {
    throw "Missing virtual environment. Run: python -m venv .venv"
}

& ".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8080

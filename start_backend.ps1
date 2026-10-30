# start_backend.ps1 — Launch the OpenClip backend survives-accidentally-closing-the-window edition.
#
# Uses pythonw.exe (no console window, so it can't be killed by closing a terminal)
# and writes logs to backend/backend.log. Do NOT stop this while a video is
# processing — pipelines run in-memory and a restart orphans them (they'll be
# marked "error" on next boot; use the Retry button in the UI).
#
# Usage: right-click > Run with PowerShell, or:  powershell -File start_backend.ps1

$root = $PSScriptRoot
$backendDir = Join-Path $root "backend"
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$logOut = Join-Path $backendDir "backend.log"
$logErr = Join-Path $backendDir "backend-err.log"

if (-not (Test-Path -LiteralPath $pythonw)) {
    Write-Host "ERROR: not found: $pythonw" -ForegroundColor Red
    Write-Host "Run setup first (create .venv with dependencies)."
    exit 1
}

$existing = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Backend already listening on :8000 (PID $($existing.OwningProcess)). Not starting another." -ForegroundColor Yellow
    exit 0
}

$p = Start-Process -FilePath $pythonw `
    -ArgumentList "-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000" `
    -WorkingDirectory $backendDir `
    -RedirectStandardOutput $logOut -RedirectStandardError $logErr `
    -PassThru
Write-Host "Backend starting, PID $($p.Id) — http://localhost:8000" -ForegroundColor Green
Write-Host "Logs: $logOut"

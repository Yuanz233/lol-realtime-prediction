param(
    [int]$Port = 8000,
    [string]$Leagues = "lpl,lck,lec,vcs,worlds",
    [switch]$AllowManualResult,
    [switch]$LogToFile
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

foreach ($Folder in @("data", "models", "backups", "logs")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot $Folder) | Out-Null
}

$env:HOST = "0.0.0.0"
$env:PORT = [string]$Port
$env:DB_PATH = Join-Path $ProjectRoot "data\matches.sqlite3"
$env:MODEL_PATH = Join-Path $ProjectRoot "models\live.json"
$env:LIVE_PROVIDER = "lolesports"
$env:LOLESPORTS_LEAGUES = $Leagues
$env:LOLESPORTS_POLL_SECONDS = "30"
$env:LOLESPORTS_DISCOVER_SECONDS = "120"
$env:LOLESPORTS_WINDOW_DELAY_SECONDS = "40"
$env:LOLESPORTS_HISTORY_SYNC_SECONDS = "21600"
$env:LOLESPORTS_HISTORY_DAYS = "14"
$env:LOLESPORTS_HISTORY_WORKERS = "2"
$env:LOLESPORTS_HISTORY_MAX_NEW_GAMES = "20"
$env:ALLOW_MANUAL_RESULT = if ($AllowManualResult) { "true" } else { "false" }

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$SystemPython = Get-Command python -ErrorAction SilentlyContinue
$PyLauncher = Get-Command py -ErrorAction SilentlyContinue

if (Test-Path $VenvPython) {
    $PythonExe = $VenvPython
    $PythonArgs = @((Join-Path $ProjectRoot "app.py"), "--host", "0.0.0.0", "--port", [string]$Port)
} elseif ($SystemPython) {
    $PythonExe = $SystemPython.Source
    $PythonArgs = @((Join-Path $ProjectRoot "app.py"), "--host", "0.0.0.0", "--port", [string]$Port)
} elseif ($PyLauncher) {
    $PythonExe = $PyLauncher.Source
    $PythonArgs = @("-3", (Join-Path $ProjectRoot "app.py"), "--host", "0.0.0.0", "--port", [string]$Port)
} else {
    throw "Python was not found. Create .venv or add Python 3.9+ to PATH."
}

if ($LogToFile) {
    $LogPath = Join-Path $ProjectRoot "logs\server.log"
    & $PythonExe @PythonArgs *>> $LogPath
} else {
    & $PythonExe @PythonArgs
}
exit $LASTEXITCODE

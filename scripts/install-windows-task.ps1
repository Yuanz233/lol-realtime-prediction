param(
    [string]$TaskName = "LoLRealtimePrediction",
    [int]$Port = 8000,
    [string]$Leagues = "lpl,lck,lec,vcs,worlds"
)

$ErrorActionPreference = "Stop"
$StartScript = Join-Path $PSScriptRoot "start-windows.ps1"
if (-not (Test-Path $StartScript)) {
    throw "Missing start script: $StartScript"
}

$Arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Port {1} -Leagues "{2}" -LogToFile' -f $StartScript, $Port, $Leagues
$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $Arguments -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$Settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Installed and started task '$TaskName' on port $Port."
Write-Host "Log: $(Join-Path (Split-Path -Parent $PSScriptRoot) 'logs\server.log')"

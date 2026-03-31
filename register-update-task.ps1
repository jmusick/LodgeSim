param(
  [string]$TaskName = "WoW-SimC-Update",
  [string]$Time = "01:30",
  [string]$ScriptPath = "C:\Projects\LodgeSim\update-simc.ps1"
)

$ErrorActionPreference = "Stop"

$cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`""

schtasks /Create /TN $TaskName /SC DAILY /ST $Time /TR $cmd /F
Write-Host "Scheduled task '$TaskName' created for $Time"

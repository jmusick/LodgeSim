param(
    [string]$EnvFile = ".env.simrunner.local",
    [switch]$ForcePython
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$envPath = Join-Path $root $EnvFile
if (-not (Test-Path $envPath)) {
    Write-Host "Missing $EnvFile in $root" -ForegroundColor Yellow
    Write-Host "Copy .env.simrunner.local.example to .env.simrunner.local and fill in values." -ForegroundColor Yellow
    exit 1
}

Get-Content $envPath | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) { return }

    $idx = $line.IndexOf("=")
    if ($idx -lt 1) { return }

    $name = $line.Substring(0, $idx).Trim()
    $value = $line.Substring($idx + 1).Trim()

    if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
        $value = $value.Substring(1, $value.Length - 2)
    }

    [Environment]::SetEnvironmentVariable($name, $value, "Process")
}

$exePath = Join-Path $root "WoWSim Website Runner.exe"
$pyScript = Join-Path $root "website_sim_runner_gui.py"
$pythonExe = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    $pythonExe = "python"
}

Write-Host "Launching GUI with env file: $EnvFile" -ForegroundColor Cyan

if (-not $ForcePython -and (Test-Path $exePath)) {
    $exeItem = Get-Item -LiteralPath $exePath
    Write-Host "Launch target: EXE" -ForegroundColor Green
    Write-Host "Path: $exePath"
    Write-Host "LastWriteTime: $($exeItem.LastWriteTime)"
    & $exePath
    exit $LASTEXITCODE
}

if (-not (Test-Path $pyScript)) {
    Write-Host "Missing GUI script: $pyScript" -ForegroundColor Red
    Write-Host "Either deploy the EXE to this folder or run without -ForcePython." -ForegroundColor Yellow
    exit 1
}

Write-Host "Launch target: Python script" -ForegroundColor Yellow
Write-Host "Path: $pyScript"
& $pythonExe $pyScript

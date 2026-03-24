#!/usr/bin/env pwsh
<#
.SYNOPSIS
Build the WoWSim Website Runner as a standalone Windows executable using PyInstaller.
#>

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $ScriptDir ".venv\Scripts\python.exe"
$GuiScript = Join-Path $ScriptDir "website_sim_runner_gui.py"
$OutputDir = Join-Path $ScriptDir "dist"
$BuildDir = Join-Path $ScriptDir "build"
$SpecFile = Join-Path $ScriptDir "WoWSim Website Runner.spec"

if (-not (Test-Path $PythonExe)) {
    Write-Error "Python executable not found at: $PythonExe"
    exit 1
}

if (-not (Test-Path $GuiScript)) {
    Write-Error "GUI script not found at: $GuiScript"
    exit 1
}

Write-Host "Building WoWSim Website Runner executable..." -ForegroundColor Cyan
Write-Host "Output directory: $OutputDir" -ForegroundColor Gray

# Create the PyInstaller spec file if it doesn't exist
if (-not (Test-Path $SpecFile)) {
    Write-Host "Creating PyInstaller spec file..." -ForegroundColor Yellow
    & $PythonExe -m PyInstaller `
        --onefile `
        --windowed `
        --name "WoWSim Website Runner" `
        --distpath $OutputDir `
        --buildpath $BuildDir `
        --specpath $ScriptDir `
        --icon "$ScriptDir\icon.ico" `
        --add-data "$ScriptDir\tier_source_overrides.json`:." `
        --add-data "$ScriptDir\config.guild.json`:." `
        --add-data "$ScriptDir\website_sim_runner.py`:.)" `
        --collect-submodules flask `
        --collect-submodules urllib3 `
        --collect-submodules simcore `
        $GuiScript
} else {
    Write-Host "Using existing spec file..." -ForegroundColor Yellow
    & $PythonExe -m PyInstaller $SpecFile
}

if ($LASTEXITCODE -eq 0) {
    $ExePath = Join-Path $OutputDir "WoWSim Website Runner.exe"
    if (Test-Path $ExePath) {
        Write-Host "✓ Build successful!" -ForegroundColor Green
        Write-Host "Executable created at: $ExePath" -ForegroundColor Green
        Write-Host ""
        Write-Host "You can now run the application by double-clicking the .exe or:" -ForegroundColor Cyan
        Write-Host "  & '$ExePath'" -ForegroundColor Gray
    } else {
        Write-Error "Executable not found after build"
        exit 1
    }
} else {
    Write-Error "PyInstaller build failed"
    exit 1
}

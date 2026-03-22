param(
  [string]$ConfigPath = ".\\config.json",
  [switch]$SkipSimcUpdate
)

$ErrorActionPreference = "Stop"
$workspace = (Resolve-Path ".").Path

if (-not $SkipSimcUpdate) {
  & (Join-Path $workspace "update-simc.ps1")
}

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$runDir = Join-Path -Path ".\\results" -ChildPath $timestamp
New-Item -Path $runDir -ItemType Directory -Force | Out-Null

$config = Get-Content -Path $ConfigPath -Raw | ConvertFrom-Json
$config.output_dir = (Resolve-Path $runDir).Path
$tempConfigPath = Join-Path -Path $runDir -ChildPath "config.runtime.json"
$config | ConvertTo-Json -Depth 8 | Set-Content -Path $tempConfigPath -Encoding UTF8

python .\\droptimizer.py --config $tempConfigPath

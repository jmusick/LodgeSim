param(
  [string]$TargetRoot = "\\Ark-Server\WoWSim",
  [string]$ExeName = "WoWSim Website Runner.exe",
  [string]$ConfigName = "config.guild.json",
  [bool]$IncludeSimc = $true,
  [bool]$IncludeCandidates = $true,
  [bool]$IncludeEnv = $true
)

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$scriptRootResolved = (Resolve-Path $scriptRoot).Path

function Resolve-JsonPathForTarget {
  param(
    [Parameter(Mandatory = $true)][string]$Value,
    [Parameter(Mandatory = $true)][string]$SourceRoot,
    [Parameter(Mandatory = $true)][string]$TargetRoot
  )

  if ([string]::IsNullOrWhiteSpace($Value)) {
    return $Value
  }

  # Keep URLs and non-file values untouched.
  if ($Value -match '^[a-zA-Z]+://') {
    return $Value
  }

  $trimChars = [char[]]@('\', '/')
  $sourceRootNormalized = $SourceRoot.TrimEnd($trimChars)
  $targetRootNormalized = $TargetRoot.TrimEnd($trimChars)

  if ([System.IO.Path]::IsPathRooted($Value)) {
    $candidate = $Value.Replace('/', '\\')
    $srcNorm = $sourceRootNormalized.Replace('/', '\\')

    if ($candidate.StartsWith($srcNorm, [System.StringComparison]::OrdinalIgnoreCase)) {
      $relative = $candidate.Substring($srcNorm.Length).TrimStart('\\')
      return (Join-Path $targetRootNormalized $relative).Replace('\\', '/')
    }

    return $Value
  }

  return (Join-Path $targetRootNormalized $Value).Replace('\\', '/')
}

function New-DirectoryIfMissing {
  param([Parameter(Mandatory = $true)][string]$Path)

  if (-not (Test-Path -LiteralPath $Path)) {
    New-Item -Path $Path -ItemType Directory -Force | Out-Null
  }
}

function Copy-OptionalFile {
  param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Destination
  )

  if (Test-Path -LiteralPath $Source) {
    $parent = Split-Path -Parent $Destination
    if ($parent) {
      New-DirectoryIfMissing -Path $parent
    }
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
  }
}

Write-Host "Preparing remote deployment to: $TargetRoot" -ForegroundColor Cyan
New-DirectoryIfMissing -Path $TargetRoot

$distExe = Join-Path $scriptRootResolved (Join-Path "dist" $ExeName)
if (-not (Test-Path -LiteralPath $distExe)) {
  throw "EXE not found at $distExe. Build first with: python -m PyInstaller 'WoWSim Website Runner.spec'"
}

$targetExe = Join-Path $TargetRoot $ExeName
Write-Host "Copying EXE -> $targetExe"
Copy-Item -LiteralPath $distExe -Destination $targetExe -Force

# Runtime helper files used by the GUI and runner.
Copy-OptionalFile -Source (Join-Path $scriptRootResolved "update-simc.ps1") -Destination (Join-Path $TargetRoot "update-simc.ps1")
Copy-OptionalFile -Source (Join-Path $scriptRootResolved "run-website-gui.ps1") -Destination (Join-Path $TargetRoot "run-website-gui.ps1")
Copy-OptionalFile -Source (Join-Path $scriptRootResolved "tier_source_overrides.json") -Destination (Join-Path $TargetRoot "tier_source_overrides.json")

# Config deployment with path rewrites for remote execution.
$sourceConfigPath = Join-Path $scriptRootResolved $ConfigName
if (-not (Test-Path -LiteralPath $sourceConfigPath)) {
  throw "Config not found: $sourceConfigPath"
}

$configJson = Get-Content -LiteralPath $sourceConfigPath -Raw | ConvertFrom-Json

if ($configJson.PSObject.Properties.Name -contains "simc_path") {
  $configJson.simc_path = Resolve-JsonPathForTarget -Value ([string]$configJson.simc_path) -SourceRoot $scriptRootResolved -TargetRoot $TargetRoot
}
if ($configJson.PSObject.Properties.Name -contains "base_profile_path") {
  $configJson.base_profile_path = Resolve-JsonPathForTarget -Value ([string]$configJson.base_profile_path) -SourceRoot $scriptRootResolved -TargetRoot $TargetRoot
}
if ($configJson.PSObject.Properties.Name -contains "candidates_path") {
  $configJson.candidates_path = Resolve-JsonPathForTarget -Value ([string]$configJson.candidates_path) -SourceRoot $scriptRootResolved -TargetRoot $TargetRoot
}
if ($configJson.PSObject.Properties.Name -contains "output_dir") {
  $configJson.output_dir = (Join-Path $TargetRoot "results/guild-runs").Replace('\\', '/')
}
if ($configJson.PSObject.Properties.Name -contains "candidates_by_spec" -and $null -ne $configJson.candidates_by_spec) {
  foreach ($p in $configJson.candidates_by_spec.PSObject.Properties) {
    $p.Value = Resolve-JsonPathForTarget -Value ([string]$p.Value) -SourceRoot $scriptRootResolved -TargetRoot $TargetRoot
  }
}

$targetConfigPath = Join-Path $TargetRoot $ConfigName
($configJson | ConvertTo-Json -Depth 20) | Set-Content -LiteralPath $targetConfigPath -Encoding UTF8
Write-Host "Wrote config -> $targetConfigPath"

# Optional data files expected by config.
Copy-OptionalFile -Source (Join-Path $scriptRootResolved "input\character.simc") -Destination (Join-Path $TargetRoot "input\character.simc")

if ($IncludeCandidates) {
  $sourceCandidates = Join-Path $scriptRootResolved "generated\live-candidates"
  $targetCandidates = Join-Path $TargetRoot "generated\live-candidates"
  if (Test-Path -LiteralPath $sourceCandidates) {
    New-DirectoryIfMissing -Path $targetCandidates
    Write-Host "Copying candidates -> $targetCandidates"
    $candidateItems = Get-ChildItem -LiteralPath $sourceCandidates -Force -ErrorAction SilentlyContinue
    if ($candidateItems -and $candidateItems.Count -gt 0) {
      Copy-Item -Path (Join-Path $sourceCandidates "*") -Destination $targetCandidates -Recurse -Force
    } else {
      Write-Warning "Candidates folder is present but empty: $sourceCandidates"
    }
  } else {
    Write-Warning "Candidates folder not found at $sourceCandidates"
  }
}

if ($IncludeSimc) {
  $sourceSimc = Join-Path $scriptRootResolved "tools\simc\nightly\current"
  $targetSimc = Join-Path $TargetRoot "tools\simc\nightly\current"
  if (Test-Path -LiteralPath $sourceSimc) {
    New-DirectoryIfMissing -Path $targetSimc
    Write-Host "Copying SimC runtime -> $targetSimc"
    $simcItems = Get-ChildItem -LiteralPath $sourceSimc -Force -ErrorAction SilentlyContinue
    if ($simcItems -and $simcItems.Count -gt 0) {
      Copy-Item -Path (Join-Path $sourceSimc "*") -Destination $targetSimc -Recurse -Force
    } else {
      Write-Warning "SimC runtime folder is present but empty: $sourceSimc"
    }
  } else {
    Write-Warning "SimC runtime not found at $sourceSimc. Run update-simc.ps1 first."
  }
}

if ($IncludeEnv) {
  $sourceEnv = Join-Path $scriptRootResolved ".env.simrunner.local"
  $targetEnv = Join-Path $TargetRoot ".env.simrunner.local"

  if (Test-Path -LiteralPath $sourceEnv) {
    Copy-Item -LiteralPath $sourceEnv -Destination $targetEnv -Force
    Write-Host "Copied env file -> $targetEnv"
  } else {
    Copy-OptionalFile -Source (Join-Path $scriptRootResolved ".env.simrunner.local.example") -Destination (Join-Path $TargetRoot ".env.simrunner.local.example")
    Write-Warning "No .env.simrunner.local found at source; copied example only."
  }
}

New-DirectoryIfMissing -Path (Join-Path $TargetRoot "results\guild-runs")

Write-Host ""
Write-Host "Deployment complete." -ForegroundColor Green
Write-Host "Target: $TargetRoot"
Write-Host "EXE:    $targetExe"
Write-Host ""
Write-Host "Remote run checklist:" -ForegroundColor Cyan
Write-Host "  1) Verify $ConfigName has the desired SIMC and output paths."
Write-Host "  2) Verify .env.simrunner.local values (site URL + runner key)."
Write-Host "  3) Launch the EXE from the remote location."

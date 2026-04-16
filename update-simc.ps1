param(
  [string]$BaseUrl = "http://downloads.simulationcraft.org/nightly/",
  [ValidateSet("win64", "winarm64")]
  [string]$Arch = "win64",
  [string]$InstallRoot = ".\\tools\\simc\\nightly",
  [switch]$Force
)

$ErrorActionPreference = "Stop"

$indexUrl = "${BaseUrl}?C=M;O=D"
$installRootResolved = Join-Path -Path (Resolve-Path ".").Path -ChildPath $InstallRoot
$downloadsDir = Join-Path $installRootResolved "downloads"
$currentDir = Join-Path $installRootResolved "current"
$stagingDir = Join-Path $installRootResolved "staging"
$versionFile = Join-Path $currentDir "VERSION.txt"
$currentSimcExe = Join-Path $currentDir "simc.exe"

function Expand-SimcArchive {
  param(
    [Parameter(Mandatory = $true)][string]$ArchivePath,
    [Parameter(Mandatory = $true)][string]$DestinationPath
  )

  $extension = [System.IO.Path]::GetExtension($ArchivePath).ToLowerInvariant()
  if ($extension -eq ".zip") {
    Expand-Archive -Path $ArchivePath -DestinationPath $DestinationPath -Force
    return
  }

  $tarCmd = Get-Command tar -ErrorAction SilentlyContinue
  if ($tarCmd) {
    Write-Host "Trying extractor: tar"
    & $tarCmd.Source -xf $ArchivePath -C $DestinationPath
    if ($LASTEXITCODE -eq 0) {
      return
    }
    Write-Warning "tar extraction failed (exit $LASTEXITCODE). Falling back to 7-Zip if available."
  }

  $sevenZipCmd = Get-Command 7z -ErrorAction SilentlyContinue
  if (-not $sevenZipCmd) {
    $sevenZipCmd = Get-Command 7za -ErrorAction SilentlyContinue
  }
  if (-not $sevenZipCmd) {
    $sevenZipCmd = Get-Command 7zr -ErrorAction SilentlyContinue
  }

  if ($sevenZipCmd) {
    Write-Host "Trying extractor: $($sevenZipCmd.Name)"
    & $sevenZipCmd.Source x -y "-o$DestinationPath" $ArchivePath
    if ($LASTEXITCODE -eq 0) {
      return
    }
    throw "7-Zip extraction failed (exit $LASTEXITCODE)."
  }

  throw "Unable to extract $ArchivePath. Install BSD tar with .7z support or install 7-Zip (7z/7za/7zr in PATH)."
}

function Update-ConfigSimcPath {
  param(
    [Parameter(Mandatory = $true)][string]$WorkspaceRoot,
    [Parameter(Mandatory = $true)][string]$SimcPath
  )

  $configCandidates = @("config.json", "config.guild.json")
  foreach ($cfg in $configCandidates) {
    $cfgPath = Join-Path $WorkspaceRoot $cfg
    if (-not (Test-Path $cfgPath)) {
      continue
    }

    try {
      $config = Get-Content -Path $cfgPath -Raw | ConvertFrom-Json
      $config.simc_path = $SimcPath
      $jsonOutput = $config | ConvertTo-Json -Depth 8
      $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
      [System.IO.File]::WriteAllText($cfgPath, $jsonOutput, $utf8NoBom)
      Write-Host "Updated $cfg simc_path -> $SimcPath"
    } catch {
      Write-Warning "$cfg exists but could not be updated automatically: $($_.Exception.Message)"
    }
  }
}

New-Item -Path $downloadsDir -ItemType Directory -Force | Out-Null
New-Item -Path $currentDir -ItemType Directory -Force | Out-Null

Write-Host "Fetching nightly index: $indexUrl"
$index = Invoke-WebRequest -Uri $indexUrl -UseBasicParsing

$pattern = ('href="(simc-[^"]+-{0}\.7z)"' -f [System.Text.RegularExpressions.Regex]::Escape($Arch))
$matches = [System.Text.RegularExpressions.Regex]::Matches($index.Content, $pattern)
if ($matches.Count -eq 0) {
  throw "Could not find a nightly archive for architecture '$Arch'."
}

$latestFile = $matches[0].Groups[1].Value
$latestUrl = "${BaseUrl}${latestFile}"
Write-Host "Latest nightly: $latestFile"

$installedVersion = ""
if (Test-Path $versionFile) {
  $installedVersion = (Get-Content -Path $versionFile -Raw).Trim()
}

if (-not $Force -and $installedVersion -eq $latestFile) {
  Write-Host "SimulationCraft is already up to date ($installedVersion)."
  exit 0
}

$archivePath = Join-Path $downloadsDir $latestFile
if (-not (Test-Path $archivePath)) {
  Write-Host "Downloading $latestUrl"
  Invoke-WebRequest -Uri $latestUrl -OutFile $archivePath
} else {
  Write-Host "Using cached archive: $archivePath"
}

if (Test-Path $stagingDir) {
  Remove-Item -Path $stagingDir -Recurse -Force
}
New-Item -Path $stagingDir -ItemType Directory -Force | Out-Null

Write-Host "Extracting archive..."
try {
  Expand-SimcArchive -ArchivePath $archivePath -DestinationPath $stagingDir
} catch {
  $extractError = $_.Exception.Message
  if (Test-Path -LiteralPath $currentSimcExe) {
    Write-Warning "SimC archive extraction failed: $extractError"
    Write-Warning "Continuing with existing installed SimC at: $currentSimcExe"
    exit 0
  }
  throw
}

$simcExe = Get-ChildItem -Path $stagingDir -Filter "simc.exe" -Recurse | Select-Object -First 1
if (-not $simcExe) {
  throw "simc.exe was not found after extraction. Ensure tar supports .7z or install 7-Zip."
}

$sourceRoot = $simcExe.Directory.FullName
$tempCurrent = Join-Path $installRootResolved "current.new"
if (Test-Path $tempCurrent) {
  Remove-Item -Path $tempCurrent -Recurse -Force
}
New-Item -Path $tempCurrent -ItemType Directory -Force | Out-Null

Copy-Item -Path (Join-Path $sourceRoot "*") -Destination $tempCurrent -Recurse -Force
$latestFile | Set-Content -Path (Join-Path $tempCurrent "VERSION.txt") -Encoding UTF8
$latestUrl | Set-Content -Path (Join-Path $tempCurrent "SOURCE_URL.txt") -Encoding UTF8

if (Test-Path $currentDir) {
  Remove-Item -Path $currentDir -Recurse -Force
}
Rename-Item -Path $tempCurrent -NewName "current"

$workspaceRoot = (Resolve-Path ".").Path
$simcPath = Join-Path -Path $workspaceRoot -ChildPath "tools\simc\nightly\current\simc.exe"
Update-ConfigSimcPath -WorkspaceRoot $workspaceRoot -SimcPath $simcPath

Write-Host "Installed nightly build: $latestFile"
Write-Host "simc path: $simcPath"

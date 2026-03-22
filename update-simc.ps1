param(
  [string]$BaseUrl = "http://downloads.simulationcraft.org/nightly/",
  [ValidateSet("win64", "winarm64")]
  [string]$Arch = "win64",
  [string]$InstallRoot = ".\\tools\\simc\\nightly",
  [switch]$Force
)

$ErrorActionPreference = "Stop"

$indexUrl = "${BaseUrl}?C=M;O=D"
$installRootResolved = (Resolve-Path "." ).Path | Join-Path -ChildPath $InstallRoot
$downloadsDir = Join-Path $installRootResolved "downloads"
$currentDir = Join-Path $installRootResolved "current"
$stagingDir = Join-Path $installRootResolved "staging"
$versionFile = Join-Path $currentDir "VERSION.txt"

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
tar -xf $archivePath -C $stagingDir

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
$configPath = Join-Path $workspaceRoot "config.json"
$simcPath = Join-Path $workspaceRoot "tools\\simc\\nightly\\current\\simc.exe"

if (Test-Path $configPath) {
  try {
    $config = Get-Content -Path $configPath -Raw | ConvertFrom-Json
    $config.simc_path = $simcPath
    $config | ConvertTo-Json -Depth 8 | Set-Content -Path $configPath -Encoding UTF8
    Write-Host "Updated config.json simc_path -> $simcPath"
  } catch {
    Write-Warning "config.json exists but could not be updated automatically: $($_.Exception.Message)"
  }
}

Write-Host "Installed nightly build: $latestFile"
Write-Host "simc path: $simcPath"

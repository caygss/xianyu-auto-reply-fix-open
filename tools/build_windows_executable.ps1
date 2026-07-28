[CmdletBinding()]
param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
# PyInstaller onedir payload: the executable and its sibling runtime folders are distributed together.
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot "venv\Scripts\python.exe"
$pyinstaller = Join-Path $repoRoot "venv\Scripts\pyinstaller.exe"
$buildRoot = Join-Path $repoRoot "build\windows-executable"
$distRoot = Join-Path $repoRoot "dist\compiled-windows"
$appRoot = Join-Path $distRoot "XianyuAutoDelivery"
$specFile = Join-Path $repoRoot "tools\xianyu_auto_delivery.spec"
$browserSource = Join-Path $env:LOCALAPPDATA "ms-playwright"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Missing venv Python: $python"
}
if (-not (Test-Path -LiteralPath $pyinstaller)) {
    throw "Missing PyInstaller. Run: venv\Scripts\python.exe -m pip install pyinstaller"
}
if (-not (Test-Path -LiteralPath $specFile)) {
    throw "Missing PyInstaller spec: $specFile"
}
if (-not (Test-Path -LiteralPath $browserSource)) {
    throw "Missing Playwright browser directory: $browserSource"
}

if ($Clean) {
    foreach ($target in @($buildRoot, $distRoot)) {
        if (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Recurse -Force
        }
    }
}

New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
New-Item -ItemType Directory -Path $distRoot -Force | Out-Null

& $pyinstaller --noconfirm --clean `
    --distpath $distRoot `
    --workpath $buildRoot `
    $specFile
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$internalRoot = Join-Path $appRoot "_internal"
foreach ($assetName in @("static", "global_config.yml", "announcement.json")) {
    $assetPath = Join-Path $internalRoot $assetName
    if (-not (Test-Path -LiteralPath $assetPath)) {
        throw "Missing bundled asset: $assetPath"
    }
    Copy-Item -LiteralPath $assetPath -Destination (Join-Path $appRoot $assetName) -Recurse -Force
}

$nodeCommand = Get-Command node -ErrorAction SilentlyContinue
if (-not $nodeCommand) {
    throw "Node.js is required for the compiled package build"
}
$nodeRoot = Join-Path $appRoot "node"
New-Item -ItemType Directory -Path $nodeRoot -Force | Out-Null
Copy-Item -LiteralPath $nodeCommand.Source -Destination (Join-Path $nodeRoot "node.exe") -Force

$payloadBrowserRoot = Join-Path $appRoot "playwright"
New-Item -ItemType Directory -Path $payloadBrowserRoot -Force | Out-Null
$dryRunText = (& $python -m playwright install --dry-run chromium 2>&1 | Out-String)
$browserNames = @([regex]::Matches($dryRunText, '(?:chromium(?:_headless_shell)?|ffmpeg|winldd)-\d+') | ForEach-Object { $_.Value } | Select-Object -Unique)
$browserEntries = @(
    Get-ChildItem -LiteralPath $browserSource -Directory |
        Where-Object { $browserNames -contains $_.Name }
)
if ($browserEntries.Count -eq 0) {
    throw "No Playwright browser runtime found in $browserSource"
}
foreach ($entry in $browserEntries) {
    Copy-Item -LiteralPath $entry.FullName -Destination (Join-Path $payloadBrowserRoot $entry.Name) -Recurse -Force
}
Get-ChildItem -LiteralPath $payloadBrowserRoot -Recurse -File -Filter "debug.log" -ErrorAction SilentlyContinue |
    Remove-Item -Force

$sensitiveRuntimePatterns = @(
    '*cookie*.json', '*token*.json', '*session*.json', 'storage_state*.json',
    'auth*.json', '*.db*', '*.log', '*.key'
)
$forbidden = Get-ChildItem -LiteralPath $appRoot -Force -Recurse -File |
    Where-Object {
        $fileName = $_.Name
        $_.Name -like '*.py' -or
        $_.FullName -match '[\\/](data|browser_data|logs|venv)[\\/]' -or
        (($sensitiveRuntimePatterns | Where-Object { $fileName -like $_ }) -ne $null)
    }
if ($forbidden.Count -gt 0) {
    $names = $forbidden | ForEach-Object { $_.FullName }
    throw "Compiled payload contains forbidden source or runtime files: $($names -join ', ')"
}

$hash = Get-FileHash -LiteralPath (Join-Path $appRoot "XianyuAutoDelivery.exe") -Algorithm SHA256
Write-Host "Compiled application: $appRoot"
Write-Host "Executable SHA256: $($hash.Hash)"

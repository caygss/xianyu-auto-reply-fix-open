[CmdletBinding()]
param(
    [string]$CompiledRoot = "",
    [string]$SourceRepositoryUrl = "https://github.com/caygss/xianyu-auto-reply-fix-open",
    [string]$SourceTag = "",
    [string]$SourceCommit = "",
    [string]$ModificationDate = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($CompiledRoot)) {
    $CompiledRoot = Join-Path $repoRoot "dist\compiled-windows\XianyuAutoDelivery"
}
if ([string]::IsNullOrWhiteSpace($ModificationDate)) {
    $ModificationDate = Get-Date -Format "yyyy-MM-dd"
}
if (-not (Test-Path -LiteralPath $CompiledRoot -PathType Container)) {
    throw "Compiled payload was not found: $CompiledRoot. Run tools\build_windows_executable.ps1 first."
}
foreach ($metadataName in @("SourceRepositoryUrl", "SourceTag", "SourceCommit")) {
    $metadataValue = Get-Variable -Name $metadataName -ValueOnly
    if ([string]::IsNullOrWhiteSpace($metadataValue)) {
        throw "$metadataName is required so the package can identify its corresponding source."
    }
}

$distRoot = Join-Path $repoRoot "dist"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmssfff"
$stagingGuid = [guid]::NewGuid().ToString('N')
$baseName = "xianyu-auto-reply-fix-windows-$timestamp-$stagingGuid"
$stagingRoot = Join-Path $distRoot $baseName
$zipPath = Join-Path $distRoot "$baseName.zip"

$excludedDirectoryNames = @(
    ".git", "venv", ".venv", "data", "browser_data", "logs",
    "trajectory_history", ".deepeval", "__pycache__", ".pytest_cache",
    ".cache", "cache", ".playwright-browsers", "dist"
)
$excludedFilePatterns = @(
    "*.db", "*.sqlite*", "*.db-wal", "*.db-shm", "*.db-journal",
    "*.log", "*.key", "realtime.log"
)
$sensitiveFilePatterns = @(
    ".env", "*.env", "*.key", "*.p12", "*.pfx",
    "config.local.*", "global_config.local.*", "credentials.*", "secrets.*",
    "*cookie*.json", "*token*.json", "*session*.json", "storage_state*.json",
    "auth*.json"
)

function Get-RelativePath {
    param(
        [Parameter(Mandatory = $true)][string]$BasePath,
        [Parameter(Mandatory = $true)][string]$FullPath
    )
    return $FullPath.Substring($BasePath.Length).TrimStart('\', '/')
}

function Test-ExcludedStagingPath {
    param([Parameter(Mandatory = $true)][string]$FullPath)
    $relativePath = Get-RelativePath -BasePath $stagingRoot -FullPath $FullPath
    $parts = $relativePath -split '[\\/]'
    foreach ($part in $parts) {
        if ($excludedDirectoryNames -contains $part) {
            return $true
        }
    }
    $leaf = Split-Path -Leaf $FullPath
    foreach ($pattern in ($excludedFilePatterns + $sensitiveFilePatterns)) {
        if ($leaf -like $pattern) {
            return $true
        }
    }
    return $false
}

$ownershipMarker = Join-Path $stagingRoot ".distribution-owner-$stagingGuid.marker"

function Test-SafeOwnedStagingPath {
    param([Parameter(Mandatory = $true)][string]$CandidatePath)
    try {
        $resolvedDist = (Resolve-Path -LiteralPath $distRoot -ErrorAction Stop).Path
        $resolvedStaging = (Resolve-Path -LiteralPath $CandidatePath -ErrorAction Stop).Path
        $distFull = [System.IO.Path]::GetFullPath($resolvedDist).TrimEnd('\', '/')
        $stagingFull = [System.IO.Path]::GetFullPath($resolvedStaging).TrimEnd('\', '/')
        $distPrefix = "$distFull\"
        $stagingNameMatches = (Split-Path -Leaf $stagingFull) -eq $baseName
        return $stagingFull.StartsWith($distPrefix, [System.StringComparison]::OrdinalIgnoreCase) -and $stagingNameMatches
    }
    catch {
        return $false
    }
}

$createdStaging = $false
try {
    New-Item -ItemType Directory -Path $distRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null
    $createdStaging = $true
    New-Item -ItemType File -Path $ownershipMarker -Force | Out-Null

    Get-ChildItem -LiteralPath $CompiledRoot -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $stagingRoot $_.Name) -Recurse -Force
    }

    foreach ($requiredFile in @("LICENSE", "README.md")) {
        $sourcePath = Join-Path $repoRoot $requiredFile
        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
            throw "Required distribution file was not found: $sourcePath"
        }
        Copy-Item -LiteralPath $sourcePath -Destination (Join-Path $stagingRoot $requiredFile) -Force
    }

    $launcherFiles = @(
        Get-ChildItem -LiteralPath $repoRoot -Filter "*.bat" -File |
            Where-Object {
                [System.IO.File]::ReadAllText($_.FullName) -match "(?im)^.*XianyuAutoDelivery\.exe"
            }
    )
    if ($launcherFiles.Count -ne 2) {
        throw "Expected exactly two compiled-package launcher batch files, found $($launcherFiles.Count)."
    }
    foreach ($launcherFile in $launcherFiles) {
        Copy-Item -LiteralPath $launcherFile.FullName -Destination (Join-Path $stagingRoot $launcherFile.Name) -Force
    }

    $docsRoot = Join-Path $stagingRoot "docs"
    New-Item -ItemType Directory -Path $docsRoot -Force | Out-Null
    foreach ($docName in @("windows-distribution.md", "open-source-distribution.md", "windows-republish-runbook.md")) {
        $docSource = Join-Path $repoRoot "docs\$docName"
        if (Test-Path -LiteralPath $docSource -PathType Leaf) {
            Copy-Item -LiteralPath $docSource -Destination (Join-Path $docsRoot $docName) -Force
        }
    }

    $sourcePointerLines = @(
        "# Corresponding Source",
        "",
        "This is the compiled Windows distribution of xianyu-auto-reply-fix under GNU AGPL-3.0.",
        "",
        "- Source repository: $SourceRepositoryUrl",
        "- Source tag: $SourceTag",
        "- Source commit: $SourceCommit",
        "- Build date: $ModificationDate",
        "",
        "You may obtain, modify, and redistribute the corresponding source and this package under AGPL-3.0.",
        "Keep the original copyright, license, and modification notices.",
        "",
        "This package does not contain Python source files. The repository and commit above identify the exact corresponding source."
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $stagingRoot "SOURCE-CODE.md"),
        ($sourcePointerLines -join [Environment]::NewLine),
        [System.Text.UTF8Encoding]::new($false)
    )

    $unsafeEntries = @(
        Get-ChildItem -LiteralPath $stagingRoot -Force -Recurse |
            Where-Object { Test-ExcludedStagingPath -FullPath $_.FullName }
    )
    if ($unsafeEntries.Count -gt 0) {
        $names = $unsafeEntries | ForEach-Object {
            Get-RelativePath -BasePath $stagingRoot -FullPath $_.FullName
        }
        throw "Unsafe excluded or sensitive entries found in staging: $($names -join ', ')"
    }

    $sourceFilesInPayload = @(
        Get-ChildItem -LiteralPath $stagingRoot -Force -Recurse -File |
            Where-Object { $_.Name -like "*.py" }
    )
    if ($sourceFilesInPayload.Count -gt 0) {
        throw "Python source files were found in the compiled distribution: $($sourceFilesInPayload.FullName -join ', ')"
    }
    if (Test-Path -LiteralPath $zipPath) {
        throw "Refusing to overwrite an existing archive: $zipPath"
    }

    Remove-Item -LiteralPath $ownershipMarker -Force
    Compress-Archive -Path (Join-Path $stagingRoot "*") -DestinationPath $zipPath -CompressionLevel Optimal

    $archiveEntries = @()
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
    $archive = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        $archiveEntries = @($archive.Entries)
        foreach ($entry in $archiveEntries) {
            $entryPath = $entry.FullName.Replace('/', '\')
            $entryLeaf = Split-Path -Leaf $entryPath
            if (($entryPath -split '[\\/]') | Where-Object { $excludedDirectoryNames -contains $_ }) {
                throw "Unsafe excluded directory found in archive: $entryPath"
            }
            foreach ($pattern in ($excludedFilePatterns + $sensitiveFilePatterns)) {
                if ($entryLeaf -like $pattern) {
                    throw "Unsafe sensitive file found in archive: $entryPath"
                }
            }
        }
    }
    finally {
        $archive.Dispose()
    }

    $fileCount = @(Get-ChildItem -LiteralPath $stagingRoot -Force -Recurse -File).Count
    $hash = Get-FileHash -LiteralPath (Join-Path $stagingRoot "XianyuAutoDelivery.exe") -Algorithm SHA256
    Write-Host "Distribution staging: $stagingRoot"
    Write-Host "Distribution zip: $zipPath"
    Write-Host "File count: $fileCount"
    Write-Host "Executable SHA256: $($hash.Hash)"
}
catch {
    # Cleanup is restricted to this run's uniquely owned staging directory.
    if ($createdStaging -and (Test-Path -LiteralPath $stagingRoot) -and (Test-Path -LiteralPath $ownershipMarker) -and (Test-SafeOwnedStagingPath -CandidatePath $stagingRoot)) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
    throw
}

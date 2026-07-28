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
foreach ($requiredMetadata in @("SourceRepositoryUrl", "SourceTag", "SourceCommit")) {
    if ([string]::IsNullOrWhiteSpace((Get-Variable -Name $requiredMetadata -ValueOnly))) {
        throw "$requiredMetadata is required so the package can identify its corresponding source."
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
    ".env", "*.env", "*.key", "*.pem", "*.p12", "*.pfx", "*.crt", "*.cert",
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

    # The compiled payload is the only executable content copied into the package.
    Get-ChildItem -LiteralPath $CompiledRoot -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $stagingRoot $_.Name) -Recurse -Force
    }

    foreach ($requiredFile in @(
        "LICENSE",
        "README.md",
        "首次安装闲鱼自动发货.bat",
        "启动闲鱼自动发货.bat"
    )) {
        $sourcePath = Join-Path $repoRoot $requiredFile
        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
            throw "Required distribution file was not found: $sourcePath"
        }
        Copy-Item -LiteralPath $sourcePath -Destination (Join-Path $stagingRoot $requiredFile) -Force
    }

    $docsRoot = Join-Path $stagingRoot "docs"
    New-Item -ItemType Directory -Path $docsRoot -Force | Out-Null
    foreach ($docName in @("windows-distribution.md", "open-source-distribution.md", "windows-republish-runbook.md")) {
        $docSource = Join-Path $repoRoot "docs\$docName"
        if (Test-Path -LiteralPath $docSource -PathType Leaf) {
            Copy-Item -LiteralPath $docSource -Destination (Join-Path $docsRoot $docName) -Force
        }
    }

    $sourcePointer = @"
# 对应源码 / Corresponding Source

本安装包是 `xianyu-auto-reply-fix` 的编译 Windows 分发版本，遵循 GNU AGPL-3.0。

- 源码仓库：$SourceRepositoryUrl
- 源码标签：$SourceTag
- 源码提交：$SourceCommit
- 构建日期：$ModificationDate

你可以免费获取、阅读、修改和再次分发对应源码及本安装包，但必须遵守 AGPL-3.0，并保留原作者与本项目的版权、许可证和修改声明。

本安装包不包含 Python 源文件；上述仓库和提交号是本安装包的对应源码定位信息。
"@
    [System.IO.File]::WriteAllText(
        (Join-Path $stagingRoot "SOURCE-CODE.md"),
        $sourcePointer,
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
    if ($createdStaging -and (Test-Path -LiteralPath $stagingRoot) -and (Test-Path -LiteralPath $ownershipMarker) -and (Test-SafeOwnedStagingPath -CandidatePath $stagingRoot)) {
        # Clean only a staging directory with this run's ownership marker and safe path.
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
    throw
}

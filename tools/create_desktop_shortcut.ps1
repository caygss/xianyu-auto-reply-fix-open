$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path

function Stop-WithMessage([string]$message) {
    Write-Host "[ERROR] $message"
    Read-Host "Press Enter to exit"
    exit 1
}

$launcherCandidates = @(
    Get-ChildItem -LiteralPath $projectRoot -Filter "*.bat" -File |
        Where-Object {
            $content = [System.IO.File]::ReadAllText($_.FullName)
            # The startup launcher is the only batch file that starts the compiled EXE.
            # Matching this behavior avoids confusing it with the first-run directory setup.
            $content -match '(?im)^\s*start\s+""\s+/min\s+.*APP_EXE'
        }
)

if ($launcherCandidates.Count -eq 0) {
    Stop-WithMessage "No launcher batch file containing XianyuAutoDelivery.exe was found in $projectRoot."
}

if ($launcherCandidates.Count -ne 1) {
    $names = ($launcherCandidates | ForEach-Object { $_.Name }) -join ", "
    Stop-WithMessage "Expected exactly one launcher batch file containing XianyuAutoDelivery.exe, found $($launcherCandidates.Count): $names"
}

$launcherPath = [System.IO.Path]::GetFullPath($launcherCandidates[0].FullName)
$desktopPath = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktopPath "Xianyu-Auto-Delivery.lnk"

if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
    Stop-WithMessage "Launcher not found: $launcherPath"
}

if ([string]::IsNullOrWhiteSpace($desktopPath)) {
    Stop-WithMessage "The current user's Desktop folder could not be resolved."
}

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($shortcutPath)
$link.TargetPath = $launcherPath
$link.WorkingDirectory = $projectRoot
$link.Description = "Start the Xianyu auto-delivery panel"
$link.Save()

Write-Host "Desktop shortcut created: $shortcutPath"

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path

function Stop-WithMessage([string]$message) {
    Write-Host "[ERROR] $message"
    Read-Host "Press Enter to exit"
    exit 1
}

$executablePath = Join-Path $projectRoot "XianyuAutoDelivery.exe"
$desktopPath = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktopPath "Xianyu-Auto-Delivery.lnk"

if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf)) {
    Stop-WithMessage "Application executable was not found: $executablePath"
}

if ([string]::IsNullOrWhiteSpace($desktopPath)) {
    Stop-WithMessage "The current user's Desktop folder could not be resolved."
}

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($shortcutPath)
$link.TargetPath = $executablePath
$link.WorkingDirectory = $projectRoot
$link.Description = "Open the Xianyu auto-delivery panel"
$link.Save()

Write-Host "Desktop shortcut created: $shortcutPath"

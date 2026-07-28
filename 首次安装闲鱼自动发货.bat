@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PROJECT_ROOT=%~dp0"
set "APP_EXE=%PROJECT_ROOT%XianyuAutoDelivery.exe"

if not exist "%APP_EXE%" (
    echo [ERROR] XianyuAutoDelivery.exe was not found.
    echo Please extract the complete distribution package, then run this file again.
    pause
    exit /b 1
)

for %%D in (data logs browser_data trajectory_history static\uploads\images) do (
    if not exist "%PROJECT_ROOT%%%D" mkdir "%PROJECT_ROOT%%%D" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Failed to create runtime folder: %%D
        pause
        exit /b 1
    )
    if not exist "%PROJECT_ROOT%%%D" (
        echo [ERROR] Cannot create runtime folder: %%D
        echo Extract the package to a writable folder such as Documents or Desktop.
        pause
        exit /b 1
    )
)

echo Installation completed. Python, Node.js, and Chromium are already bundled.
echo Next, double-click the startup batch file in this folder.
pause
exit /b 0

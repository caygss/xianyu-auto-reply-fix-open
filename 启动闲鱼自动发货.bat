@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PROJECT_ROOT=%~dp0"
set "APP_EXE=%PROJECT_ROOT%XianyuAutoDelivery.exe"

if not exist "%APP_EXE%" (
    echo [ERROR] XianyuAutoDelivery.exe was not found.
    echo Please extract the complete distribution package first.
    pause
    exit /b 1
)

call :probe_health
if "%SERVICE_HEALTH%"=="healthy" (
    echo The local service is already running.
    echo Opening the existing browser panel at http://127.0.0.1:8090
    start "" http://127.0.0.1:8090
    exit /b 0
)

netstat -ano -p tcp | findstr /r /c:":8090 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [ERROR] Port conflict: 8090 is occupied by another service.
    echo Stop that service or change the API port before starting this project.
    pause
    exit /b 1
)

echo Starting the bundled local service...
if not exist "%PROJECT_ROOT%logs" mkdir "%PROJECT_ROOT%logs" >nul 2>&1
start "" /min "%APP_EXE%"

echo Waiting for the service health check...
set /a HEALTH_ATTEMPTS=0
:wait_for_service
call :probe_health
if "%SERVICE_HEALTH%"=="healthy" goto :service_ready
set /a HEALTH_ATTEMPTS+=1
if %HEALTH_ATTEMPTS% GEQ 30 goto :service_failed
timeout /t 1 /nobreak >nul
goto :wait_for_service

:service_ready
start "" http://127.0.0.1:8090
echo The browser panel should now be open at http://127.0.0.1:8090
exit /b 0

:service_failed
echo [ERROR] The bundled application did not become ready within 30 seconds.
echo The browser was not opened. Check the logs folder inside this package.
pause
exit /b 1

:probe_health
set "SERVICE_HEALTH=unreachable"
for /f "usebackq delims=" %%A in (`powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; try { $response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8090/health' -TimeoutSec 2; if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300 -and $response.Content -match 'status.*healthy') { 'healthy' } elseif ($response.StatusCode -ge 100) { 'other' } else { 'unreachable' } } catch { 'unreachable' }"`) do set "SERVICE_HEALTH=%%A"
exit /b 0

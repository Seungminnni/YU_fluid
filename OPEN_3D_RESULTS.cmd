@echo off
setlocal
set "ROOT_DIR=%~dp0"
set "VIEWER_DIR=%ROOT_DIR%viewer"
set "SERVER_SCRIPT=%ROOT_DIR%serve-viewer.ps1"
set "PORT_FILE=%TEMP%\YU_fluid_viewer_%RANDOM%_%RANDOM%.port"

if not exist "%VIEWER_DIR%\results.html" (
  echo [ERROR] viewer\results.html was not found. Extract the entire ZIP first.
  pause
  exit /b 1
)

if not exist "%SERVER_SCRIPT%" (
  echo [ERROR] serve-viewer.ps1 was not found. Extract the entire ZIP first.
  pause
  exit /b 1
)

del /q "%PORT_FILE%" >nul 2>&1
start "YU Fluid Viewer Server" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%SERVER_SCRIPT%" -Root "%VIEWER_DIR%" -Port 0 -PortFile "%PORT_FILE%"

set /a WAIT_COUNT=0
:wait_for_port
if exist "%PORT_FILE%" goto port_ready
ping 127.0.0.1 -n 2 >nul
set /a WAIT_COUNT+=1
if %WAIT_COUNT% LSS 30 goto wait_for_port
echo [ERROR] The local viewer server could not start.
echo Close security software warnings, extract the entire ZIP, and try again.
pause
exit /b 1

:port_ready
set /p VIEWER_PORT=<"%PORT_FILE%"
del /q "%PORT_FILE%" >nul 2>&1
if "%VIEWER_PORT%"=="" (
  echo [ERROR] The local viewer server returned no port number.
  pause
  exit /b 1
)
set "VIEWER_URL=http://127.0.0.1:%VIEWER_PORT%/results.html"
set "HEALTH_URL=http://127.0.0.1:%VIEWER_PORT%/_cfd_twin_health"

set /a HEALTH_COUNT=0
:wait_for_health
powershell.exe -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing '%HEALTH_URL%' -TimeoutSec 2; if ($r.Content -eq 'YU_FLUID_VIEWER_OK') { exit 0 } else { exit 1 } } catch { exit 1 }"
if not errorlevel 1 goto open_viewer
ping 127.0.0.1 -n 2 >nul
set /a HEALTH_COUNT+=1
if %HEALTH_COUNT% LSS 15 goto wait_for_health
echo [ERROR] The local viewer server did not respond.
pause
exit /b 1

:open_viewer
start "" "%VIEWER_URL%"
endlocal

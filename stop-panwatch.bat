@echo off
setlocal EnableExtensions
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "LOGDIR=%ROOT%\.logs"
set "BACKEND_PORT=8000"
set "FRONTEND_PORT=5183"

echo [PanWatch] Stopping PanWatch...

for %%p in (%BACKEND_PORT% %FRONTEND_PORT%) do (
  for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":%%p" ^| findstr "LISTENING"') do (
    taskkill /PID %%a /T /F >nul 2>&1
    echo [PanWatch] Killed PID %%a on port %%p
  )
)

timeout /t 1 >nul

set "STILL=0"
for %%p in (%BACKEND_PORT% %FRONTEND_PORT%) do (
  netstat -ano 2>nul | findstr ":%%p" | findstr "LISTENING" >nul
  if not errorlevel 1 set "STILL=1"
)

if "%STILL%"=="1" goto :still
goto :clean

:still
echo [PanWatch] A process is still listening on 8000/5183. Check manually.
goto :end

:clean
echo [PanWatch] Ports released. PanWatch stopped.

:end
pause

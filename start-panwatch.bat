@echo off
setlocal EnableExtensions
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "LOGDIR=%ROOT%\.logs"
set "BACKEND_PORT=8000"
set "FRONTEND_PORT=5183"

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

set "OCCUPIED=0"
netstat -ano 2>nul | findstr ":%BACKEND_PORT%" | findstr "LISTENING" >nul
if not errorlevel 1 set "OCCUPIED=1"
netstat -ano 2>nul | findstr ":%FRONTEND_PORT%" | findstr "LISTENING" >nul
if not errorlevel 1 set "OCCUPIED=1"
if "%OCCUPIED%"=="1" goto :portbusy
goto :main

:portbusy
echo [PanWatch] Port 8000 or 5183 is occupied. Run stop-panwatch.bat first.
goto :end

:main
set "BELOG=%LOGDIR%\backend.log"
(
  echo @echo off
  echo set DEV_RELOAD=1
  echo cd /d "%ROOT%"
  echo call "%ROOT%\.venv\Scripts\activate.bat" 2^>nul
  echo "%ROOT%\.venv\Scripts\python.exe" server.py ^>^> "%BELOG%" 2^>^&1
) > "%LOGDIR%\run-backend.bat"

set "FELOG=%LOGDIR%\frontend.log"
(
  echo @echo off
  echo cd /d "%ROOT%\frontend"
  echo where pnpm ^>nul 2^>^&1
  echo if errorlevel 1 ^(
  echo   node "%ROOT%\frontend\node_modules\vite\bin\vite.js" ^>^> "%FELOG%" 2^>^&1
  echo ^) else ^(
  echo   pnpm dev ^>^> "%FELOG%" 2^>^&1
  echo ^)
) > "%LOGDIR%\run-frontend.bat"

echo [PanWatch] Starting backend and frontend...
start "" /min "%LOGDIR%\run-backend.bat"
start "" /min "%LOGDIR%\run-frontend.bat"

set "READY=0"
for /l %%i in (1,1,40) do (
  if not "%READY%"=="1" (
    curl -s -m 2 "http://127.0.0.1:%BACKEND_PORT%/api/health" 2>nul | findstr "status" >nul
    if not errorlevel 1 set "READY=1"
  )
)

if "%READY%"=="1" goto :ready
goto :notready

:ready
echo [PanWatch] Backend ready: http://127.0.0.1:%BACKEND_PORT%/api/health
goto :finish

:notready
echo [PanWatch] Backend not ready within timeout. Check %BELOG%

:finish
echo [PanWatch] Frontend: http://127.0.0.1:%FRONTEND_PORT%  (log: %FELOG%)
echo [PanWatch] Done. To stop, run stop-panwatch.bat

:end
pause

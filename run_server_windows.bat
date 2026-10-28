@echo off
setlocal

REM Windows launcher for the MGW_Kompy dashboard.
REM Edit SERVER_IP if this Windows PC's fixed LAN address changes.
set "PORT=8000"
set "SERVER_IP=192.168.169.2"

cd /d "%~dp0"

if not exist "%~dp0server\inventory_server.py" (
    echo [ERROR] Cannot find server\inventory_server.py next to this launcher.
    echo Keep this .bat file in the MGW_Kompy project root.
    pause
    exit /b 1
)

REM Avoid starting a second copy if the dashboard is already listening locally.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$c=New-Object Net.Sockets.TcpClient; try {$c.Connect('127.0.0.1',%PORT%); exit 0} catch {exit 1} finally {$c.Dispose()}" >nul 2>&1
if not errorlevel 1 (
    echo MGW_Kompy dashboard is already running on port %PORT%.
    echo Open http://clonezilla:%PORT% in your browser.
    exit /b 0
)

where py >nul 2>&1
if not errorlevel 1 goto run_py
where python >nul 2>&1
if not errorlevel 1 goto run_python

echo [ERROR] Python 3 was not found. Install Python 3, then run this file again.
pause
exit /b 1

:run_py
echo Starting MGW_Kompy on %SERVER_IP%:%PORT% ...
echo Keep this window open while using the dashboard.
echo Dashboard: http://clonezilla:%PORT%
py -3 "%~dp0server\inventory_server.py" --host 0.0.0.0 --port %PORT% --server-ip %SERVER_IP% --output "%~dp0server\inventory.csv" --pool "%~dp0server\ip_pool.csv" --config "%~dp0server\network_config.json"
goto finished

:run_python
echo Starting MGW_Kompy on %SERVER_IP%:%PORT% ...
echo Keep this window open while using the dashboard.
echo Dashboard: http://clonezilla:%PORT%
python "%~dp0server\inventory_server.py" --host 0.0.0.0 --port %PORT% --server-ip %SERVER_IP% --output "%~dp0server\inventory.csv" --pool "%~dp0server\ip_pool.csv" --config "%~dp0server\network_config.json"

:finished
if errorlevel 1 (
    echo.
    echo [ERROR] The dashboard stopped. Read the error above.
    pause
)
endlocal

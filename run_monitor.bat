@echo off
setlocal
cd /d "%~dp0"

:loop
echo [%date% %time%] Starting monitor.py >> monitor_wrapper.log
python monitor.py >> monitor_wrapper.log 2>&1
echo [%date% %time%] monitor.py exited with code %errorlevel%, restarting in 10s >> monitor_wrapper.log
timeout /t 10 /nobreak >nul
goto loop

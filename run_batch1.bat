@echo off
REM Batch1 wrapper for Windows Task Scheduler.
REM Uses %~dp0 (this file's own folder) so no non-ASCII path is stored in the .bat.
cd /d "%~dp0"
if not exist logs mkdir logs
echo ==== batch1 start %date% %time% ==== >> "logs\batch1.log"
"%~dp0.venv\Scripts\python.exe" main.py >> "logs\batch1.log" 2>&1
echo ==== batch1 end (exit %errorlevel%) ==== >> "logs\batch1.log"

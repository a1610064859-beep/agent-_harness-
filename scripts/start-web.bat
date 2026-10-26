@echo off
cd /d "%~dp0.."
chcp 65001 >nul
set PYTHONUTF8=1
if not exist .venv\Scripts\python.exe call "%~dp0setup.bat" nopause || (pause & exit /b 1)
echo Starting http://127.0.0.1:8765/  (close this window or press Ctrl+C to stop)
start "" /b cmd /c "timeout /t 3 >nul & start "" http://127.0.0.1:8765/"
.venv\Scripts\python -m fiscal_harness serve --port 8765
pause

@echo off
cd /d "%~dp0.."
chcp 65001 >nul
set PYTHONUTF8=1
start "" /b cmd /c "timeout /t 3 >nul & start "" http://127.0.0.1:8765/"
.venv\Scripts\python -m fiscal_harness serve --port 8765

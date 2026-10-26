@echo off
cd /d "%~dp0.."
chcp 65001 >nul
set PYTHONUTF8=1
if not exist .venv\Scripts\python.exe call "%~dp0setup.bat" nopause || (pause & exit /b 1)
.venv\Scripts\python -m pytest -q
if "%1"=="--eval" .venv\Scripts\python -m evals.run --repeat 1
pause

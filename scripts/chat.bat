@echo off
rem Usage: scripts\chat.bat --user U001 --company C001
cd /d "%~dp0.."
chcp 65001 >nul
set PYTHONUTF8=1
if not exist .venv\Scripts\python.exe call "%~dp0setup.bat" nopause || (pause & exit /b 1)
.venv\Scripts\python -m fiscal_harness chat %*
pause

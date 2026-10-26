@echo off
rem One-time setup: venv, dependencies, .env, database
cd /d "%~dp0.."
chcp 65001 >nul
set PYTHONUTF8=1
set PY=python
where py >nul 2>nul && set PY=py -3
if not exist .venv\Scripts\python.exe %PY% -m venv .venv || goto :fail
.venv\Scripts\python -m pip install -r requirements.txt || goto :fail
if not exist .env copy .env.example .env >nul && echo Created .env - put your LLM_API_KEY in it.
if not exist data\harness.db .venv\Scripts\python -m fiscal_harness init || goto :fail
echo.
echo Setup done. Next: scripts\start-web.bat  or  scripts\chat.bat
exit /b 0
:fail
echo Setup failed. Python 3.10+ is required.
exit /b 1

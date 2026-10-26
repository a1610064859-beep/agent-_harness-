@echo off
cd /d "%~dp0.."
chcp 65001 >nul
set PYTHONUTF8=1
.venv\Scripts\python -m pytest -q
if "%1"=="--eval" .venv\Scripts\python -m evals.run --repeat 1

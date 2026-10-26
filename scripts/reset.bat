@echo off
rem Restore the official sample data (clears saved edits, memories, tasks and run logs)
cd /d "%~dp0.."
chcp 65001 >nul
set PYTHONUTF8=1
.venv\Scripts\python -m fiscal_harness init --reset

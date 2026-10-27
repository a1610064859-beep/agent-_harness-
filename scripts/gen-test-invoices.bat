@echo off
rem Usage: scripts\gen-test-invoices.bat --company C001 --month 2026-09 -n 5 [--defect upper]
rem Writes watermarked test samples (TEST numbers) to test-invoices\, then opens the folder.
cd /d "%~dp0.."
chcp 65001 >nul
set PYTHONUTF8=1
if not exist .venv\Scripts\python.exe call "%~dp0setup.bat" nopause || (pause & exit /b 1)
.venv\Scripts\python -m fiscal_harness data gen-test-invoices %*
if exist test-invoices start "" test-invoices
pause

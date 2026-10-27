@echo off
rem Usage: scripts\ocr-check.bat <folder of e-invoice PDFs that have a text layer>
rem Renders each PDF to images (clean + bad scan), recognizes them, and compares with the PDF's own text layer.
cd /d "%~dp0.."
chcp 65001 >nul
set PYTHONUTF8=1
if not exist .venv\Scripts\python.exe call "%~dp0setup.bat" nopause || (pause & exit /b 1)
.venv\Scripts\python scripts\ocr_check.py %*
pause

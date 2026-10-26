@echo off
setlocal
rem One-time setup: Python venv, dependencies, .env, database.
rem If no Python 3.10+ is installed, uv is downloaded into .tools\ and fetches a private Python 3.12
rem (no admin rights, nothing changed system-wide). First run needs internet.
cd /d "%~dp0.."
chcp 65001 >nul
set PYTHONUTF8=1
set "VPY=.venv\Scripts\python.exe"
if exist "%VPY%" goto :deps

set "PY="
for %%C in ("py -3" "python" "python3") do (
  if not defined PY (
    %%~C -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=%%~C"
  )
)
if not defined PY goto :uv
echo [setup] Using %PY%
%PY% -m venv .venv || goto :fail
goto :deps

:uv
echo [setup] No Python 3.10+ found. Downloading uv to fetch a private Python 3.12 ...
set "UV="
for /f "delims=" %%F in ('dir /s /b .tools\uv\uv.exe 2^>nul') do set "UV=%%F"
if defined UV goto :uvvenv
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol='Tls12'; New-Item -ItemType Directory -Force '.tools\uv' | Out-Null; Invoke-WebRequest -UseBasicParsing 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile '.tools\uv.zip'; Expand-Archive -Force '.tools\uv.zip' '.tools\uv'; Remove-Item '.tools\uv.zip'" || goto :fail
for /f "delims=" %%F in ('dir /s /b .tools\uv\uv.exe 2^>nul') do set "UV=%%F"
if not defined UV goto :fail
:uvvenv
"%UV%" venv .venv --python 3.12 --seed || goto :fail

:deps
if not exist "%VPY%" goto :fail
echo [setup] Installing dependencies ...
"%VPY%" -m pip install -q -r requirements.txt || "%VPY%" -m pip install -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple || goto :fail
if not exist .env (
  copy .env.example .env >nul
  echo [setup] Created .env - put your LLM_API_KEY in it.
)
if not exist data\harness.db (
  "%VPY%" -m fiscal_harness init || goto :fail
)
echo.
echo [setup] Done. Next: scripts\start-web.bat  or  scripts\chat.bat
if /i not "%~1"=="nopause" pause
exit /b 0

:fail
echo.
echo [setup] FAILED - see the messages above. The first run needs internet access.
if /i not "%~1"=="nopause" pause
exit /b 1

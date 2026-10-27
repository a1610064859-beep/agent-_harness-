@echo off
setlocal
rem Optional, one-time: scanned/image invoice recognition with MinerU (runs only on this PC).
rem Usage: scripts\setup-ocr.bat [standard^|basic]     (default: standard; uses the GPU via Vulkan when present)
rem - MinerU 4.0.12 goes into its own venv .ocr\venv (many packages; kept apart from the harness .venv)
rem - downloads the tier's models (standard about 2 GB; falls back to ModelScope if HuggingFace fails)
rem - turns MinerU telemetry off and keeps parsing local (never uses the mineru.net remote service)
rem - installs the QR reader into .venv and writes FH_OCR / FH_MINERU_BIN / FH_OCR_TIER into .env
cd /d "%~dp0.."
chcp 65001 >nul
set PYTHONUTF8=1
set "TIER=%~1"
if "%TIER%"=="" set "TIER=standard"
if not exist .venv\Scripts\python.exe call "%~dp0setup.bat" nopause || goto :fail

set "UV="
for /f "delims=" %%F in ('dir /s /b .tools\uv\uv.exe 2^>nul') do set "UV=%%F"
if defined UV goto :venv
echo [setup-ocr] Downloading uv ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol='Tls12'; New-Item -ItemType Directory -Force '.tools\uv' | Out-Null; Invoke-WebRequest -UseBasicParsing 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile '.tools\uv.zip'; Expand-Archive -Force '.tools\uv.zip' '.tools\uv'; Remove-Item '.tools\uv.zip'" || goto :fail
for /f "delims=" %%F in ('dir /s /b .tools\uv\uv.exe 2^>nul') do set "UV=%%F"
if not defined UV goto :fail

:venv
set "M=%CD%\.ocr\venv\Scripts\mineru.exe"
set "MK=%CD%\.ocr\venv\Scripts\mineru-kit.exe"
if exist "%M%" goto :models
echo [setup-ocr] Installing MinerU 4.0.12 into .ocr\venv (several minutes) ...
"%UV%" venv .ocr\venv --python 3.12 || goto :fail
"%UV%" pip install --python .ocr\venv\Scripts\python.exe "mineru==4.0.12" || "%UV%" pip install --python .ocr\venv\Scripts\python.exe "mineru==4.0.12" --index-url https://pypi.tuna.tsinghua.edu.cn/simple || goto :fail

:models
echo [setup-ocr] Installing the QR reader into .venv ...
.venv\Scripts\python -m pip install -q -r requirements-ocr.txt || .venv\Scripts\python -m pip install -q -r requirements-ocr.txt -i https://pypi.tuna.tsinghua.edu.cn/simple || goto :fail
echo [setup-ocr] Downloading %TIER% models ...
"%MK%" models download --tier %TIER% --source auto || "%MK%" models download --tier %TIER% --source modelscope || goto :fail

if not exist data\ocr mkdir data\ocr
pushd data\ocr
"%M%" server start
"%M%" telemetry disable
"%M%" config set parse_server.local.mode managed || (popd & goto :fail)
"%M%" config set parse_server.local.managed_tier %TIER% || (popd & goto :fail)
"%M%" server stop
"%M%" server start
popd
.venv\Scripts\python scripts\ocr_env.py "%M%" %TIER% || goto :fail
echo.
echo [setup-ocr] Done. Check accuracy and speed on this PC:  scripts\ocr-check.bat
echo [setup-ocr] Then import scans as usual (web import tab, or: data import-files --confirm-ocr).
if /i not "%~2"=="nopause" pause
exit /b 0

:fail
echo.
echo [setup-ocr] FAILED - see the messages above. Needs internet access the first time.
if /i not "%~2"=="nopause" pause
exit /b 1

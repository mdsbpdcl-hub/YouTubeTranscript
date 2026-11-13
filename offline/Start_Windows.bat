@echo off
setlocal EnableExtensions
title YouTube Transcript Extractor (offline)
cd /d "%~dp0"
set "YTE_OFFLINE=1"

echo.
echo  ==========================================================
echo    YouTube Transcript Extractor - runs on this computer
echo  ==========================================================
echo.

rem ---- 1. Find Python 3.10 or newer ----------------------------------------
set "PY="
for %%V in (3.12 3.13 3.11 3.10) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""

if not defined PY (
  echo  Python is not installed. Installing Python 3.12 now ^(one time only^)...
  echo.
  winget install --id Python.Python.3.12 -e --scope user --accept-package-agreements --accept-source-agreements
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""
)
if not defined PY (
  echo.
  echo  Could not install Python automatically.
  echo  The download page will open now. Install Python 3.12 and TICK
  echo  "Add python.exe to PATH" on the first screen, then double-click
  echo  this file again.
  start "" "https://www.python.org/downloads/windows/"
  pause
  exit /b 1
)

rem ---- 2. One-time setup (private folder .venv, about 2-4 minutes) --------
if not exist ".venv\Scripts\python.exe" (
  echo  First-time setup: preparing the app. This takes a few minutes...
  %PY% -m venv .venv
  if errorlevel 1 goto :fail
)
fc /b requirements.txt ".venv\installed-requirements.txt" >nul 2>&1
if errorlevel 1 (
  echo  Installing / updating components...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet --disable-pip-version-check
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet --disable-pip-version-check
  if errorlevel 1 goto :fail
  copy /y requirements.txt ".venv\installed-requirements.txt" >nul
)

rem ---- 3. Start the app (browser opens automatically) ----------------------
if not exist "%USERPROFILE%\.streamlit\credentials.toml" (
  mkdir "%USERPROFILE%\.streamlit" >nul 2>&1
  > "%USERPROFILE%\.streamlit\credentials.toml" (echo [general]& echo email = "")
)
echo.
echo  Starting... your browser will open at http://localhost:8501
echo  Keep this window open while you use the app. Close it to stop.
echo.
".venv\Scripts\python.exe" -m streamlit run streamlit_app.py --server.headless false --server.port 8501 --browser.gatherUsageStats false --logger.hideWelcomeMessage true
goto :eof

:fail
echo.
echo  Setup failed. Check the internet connection and run this file again.
echo  If it keeps failing, delete the ".venv" folder and try once more.
pause
exit /b 1

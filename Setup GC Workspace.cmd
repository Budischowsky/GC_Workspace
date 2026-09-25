@echo off
setlocal
cd /d "%~dp0"
title GC Workspace - setup
echo Setting up GC Workspace for this PC ...
set "GCWS_PY="
py -3.14 -c "import sys" >nul 2>&1
if not errorlevel 1 set "GCWS_PY=py -3.14"
if not defined GCWS_PY (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
    if not errorlevel 1 set "GCWS_PY=python"
)
if not defined GCWS_PY (
    echo Installing Python 3.14 with the Windows package manager ...
    winget install --exact --id Python.Python.3.14 --source winget --accept-package-agreements --accept-source-agreements
    if errorlevel 1 goto missing
    echo Please run this setup again so the new Python is found.
    pause
    exit /b 0
)
%GCWS_PY% setup_gcws.py
if errorlevel 1 (
    echo Please check the message above.
    pause
    exit /b 1
)
pause
exit /b 0
:missing
echo Install Python 3.14 from https://www.python.org/downloads/windows/ and run this setup again.
pause
exit /b 1

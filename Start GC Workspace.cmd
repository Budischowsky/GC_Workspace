@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "GC Workspace.pyw" %*
    exit /b 0
)
where pythonw >nul 2>&1
if not errorlevel 1 (
    start "" pythonw "GC Workspace.pyw" %*
    exit /b 0
)
echo No Python found. Please run "Setup GC Workspace.cmd" first.
pause
exit /b 1

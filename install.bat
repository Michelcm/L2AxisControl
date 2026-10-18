@echo off
rem L2Axis Control - double-click to install (checks Python, installs pywebview, creates shortcuts)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause

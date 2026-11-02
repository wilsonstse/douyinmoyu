@echo off
cd /d "%~dp0"
python desktop.py
if errorlevel 1 (
  echo.
  echo Failed to start. Please copy the error above and send it to me.
  pause
)

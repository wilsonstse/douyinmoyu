@echo off
cd /d "%~dp0"
if not exist "dist\remote.exe" (
  echo Missing dist\remote.exe. Please download the complete repository.
  pause
  exit /b 1
)
"dist\remote.exe"
if errorlevel 1 (
  echo.
  echo Failed to start remote.exe.
  pause
)

@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Splitter MVP - BYN + QR
where python >nul 2>nul
if errorlevel 1 (
  echo Python not found. Install Python 3.10 or newer: https://www.python.org/downloads/
  pause
  exit /b 1
)
echo Installing QR requirements if needed...
python -m pip install -r requirements.txt
if errorlevel 1 (
 echo Could not install QR dependency. See README.md
 pause
 exit /b 1
)
echo.
echo Open on this computer: http://localhost:8765
echo Share the public URL printed by the server with phones on the same Wi-Fi.
echo Press Ctrl+C to stop the server.
start "" cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:8765"
python server.py
pause

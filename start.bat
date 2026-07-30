@echo off
:: Self-elevate to admin if not already
net session >nul 2>&1
if errorlevel 1 (
    powershell -Command "Start-Process '%~f0' -Verb RunAs -WorkingDirectory '%~dp0'"
    exit /b
)

cd /d "%~dp0"

echo ========================================
echo   KoeLog Starting...
echo ========================================
echo.

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found.
    echo Please run install.bat first.
    pause
    exit /b 1
)

if exist "ffmpeg\bin\ffmpeg.exe" (
    set "PATH=%~dp0ffmpeg\bin;%PATH%"
    echo [OK] ffmpeg: local
) else (
    where ffmpeg >nul 2>&1
    if not errorlevel 1 (
        echo [OK] ffmpeg: system
    ) else (
        echo [NOTE] ffmpeg not found (preprocessing disabled)
    )
)

echo.
echo   Open http://localhost:8000 in your browser
echo   Close this window to stop the server
echo.

venv\Scripts\python -m uvicorn app:app --host 0.0.0.0 --port 8000
pause

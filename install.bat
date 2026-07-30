@echo off
:: Self-elevate to admin if not already
net session >nul 2>&1
if errorlevel 1 (
    powershell -Command "Start-Process '%~f0' -Verb RunAs -WorkingDirectory '%~dp0'"
    exit /b
)

cd /d "%~dp0"

echo ========================================
echo   KoeLog Installer
echo ========================================
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found.
    echo Please install Python 3.9 or later.
    echo https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [OK] Python found
python --version

REM Create virtual environment
if not exist "venv" (
    echo.
    echo [1/3] Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [OK] Virtual environment created
) else (
    echo [OK] Virtual environment already exists
)

REM Install packages
echo.
echo [2/3] Installing packages...
echo       (First run will download Whisper model ~500MB)
venv\Scripts\pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Package installation failed.
    pause
    exit /b 1
)
echo [OK] Packages installed

REM Check/Download ffmpeg
echo.
echo [3/3] Checking ffmpeg...
where ffmpeg >nul 2>&1
if not errorlevel 1 (
    echo [OK] ffmpeg found in PATH
    goto :ffmpeg_done
)

if exist "ffmpeg\bin\ffmpeg.exe" (
    echo [OK] ffmpeg already downloaded
    goto :ffmpeg_done
)

echo       Downloading ffmpeg...
if not exist "ffmpeg" mkdir ffmpeg

curl -L -o ffmpeg\ffmpeg.zip https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip
if errorlevel 1 (
    echo [WARN] ffmpeg download failed.
    echo        KoeLog will still work, but audio preprocessing will be skipped.
    goto :ffmpeg_done
)

echo       Extracting...
powershell -Command "Expand-Archive -Path 'ffmpeg\ffmpeg.zip' -DestinationPath 'ffmpeg\temp' -Force"

for /d %%d in (ffmpeg\temp\ffmpeg-*) do (
    if exist "%%d\bin" (
        xcopy "%%d\bin\*" "ffmpeg\bin\" /E /Y /Q >nul
    )
)

del ffmpeg\ffmpeg.zip 2>nul
rmdir /s /q ffmpeg\temp 2>nul

if exist "ffmpeg\bin\ffmpeg.exe" (
    echo [OK] ffmpeg installed
) else (
    echo [WARN] ffmpeg extraction failed. Please install manually.
)

:ffmpeg_done

echo.
echo ========================================
echo   Installation complete!
echo ========================================
echo.
echo   Start: double-click start.bat
echo   Browser: http://localhost:8000
echo.
pause

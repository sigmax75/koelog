@echo off
echo Stopping KoeLog...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    taskkill /f /pid %%p >nul 2>&1
)
echo Done.
timeout /t 2

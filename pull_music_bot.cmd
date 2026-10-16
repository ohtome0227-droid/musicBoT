@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 exit /b 1

where git >nul 2>&1
if errorlevel 1 (
    echo Git was not found. Install Git or add it to PATH.
    if /i not "%~1"=="--no-pause" pause
    exit /b 1
)

echo Pulling updates from origin/main...
git pull --ff-only origin main
set "PULL_EXIT=%ERRORLEVEL%"
echo.
if not "%PULL_EXIT%"=="0" (
    echo Update failed. Check the Git error above.
) else (
    echo Update check completed. Restart the bot to use updated code.
)
if /i not "%~1"=="--no-pause" pause
exit /b %PULL_EXIT%

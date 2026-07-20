@echo off
setlocal
cd /d "%~dp0"

echo ResolveOps CLI
echo API-first Agent for enterprise order exception handling.
echo.

python --version >nul 2>&1
if errorlevel 1 (
  echo [Error] Python was not found. Install Python 3.12+ and reopen this file.
  echo.
  cmd /k
  exit /b 1
)

python resolveops.py init
echo.
python resolveops.py status
echo.

echo Common commands:
echo   python resolveops.py case list
echo   python resolveops.py case show ^<case-id^>
echo   python resolveops.py case chat ^<case-id^>
echo.
echo If authentication failed, edit:
echo   %USERPROFILE%\.resolveops\config.json
echo.

cmd /k

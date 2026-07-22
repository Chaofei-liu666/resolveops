@echo off
setlocal
cd /d "%~dp0"

echo ResolveOps screen-recording demo
echo.

python --version >nul 2>&1
if errorlevel 1 (
  echo [Error] Python was not found. Install Python 3.12+ and reopen this file.
  echo.
  cmd /k
  exit /b 1
)

docker --version >nul 2>&1
if errorlevel 1 (
  echo [Error] Docker was not found. Start Docker Desktop or install Docker, then reopen this file.
  echo.
  cmd /k
  exit /b 1
)

docker info >nul 2>&1
if errorlevel 1 (
  echo [Error] Docker Desktop is not ready.
  echo Start Docker Desktop first, wait until the engine is running, then reopen this file.
  echo.
  cmd /k
  exit /b 1
)

echo [Docker] Starting ResolveOps services if needed...
docker compose up -d
if errorlevel 1 (
  echo.
  echo [Error] Could not start ResolveOps services.
  echo Try manually:
  echo   docker compose up -d
  echo.
  cmd /k
  exit /b 1
)

echo.
echo [Demo] Running scripted CLI demo...
python scripts\demo_record.py

echo.
echo Demo finished. You can keep this window open for recording.
cmd /k

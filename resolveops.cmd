@echo off
setlocal
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
  echo [Error] Python was not found. Install Python 3.12+ and reopen this file.
  echo.
  cmd /k
  exit /b 1
)

python resolveops.py init
echo.

docker --version >nul 2>&1
if errorlevel 1 (
  echo [Error] Docker was not found. Start Docker Desktop or install Docker, then reopen this file.
  echo.
  cmd /k
  exit /b 1
)

docker compose version >nul 2>&1
if errorlevel 1 (
  docker-compose version >nul 2>&1
  if errorlevel 1 (
    echo [Error] Docker Compose was not available. Start Docker Desktop, then reopen this file.
    echo.
    cmd /k
    exit /b 1
  )
  set "COMPOSE_CMD=docker-compose"
) else (
  set "COMPOSE_CMD=docker compose"
)

docker info >nul 2>&1
if errorlevel 1 (
  echo [Docker] Docker daemon is not ready. Trying to start Docker Desktop...
  if exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" (
    start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
  ) else (
    echo [Error] Docker Desktop was not found at:
    echo   %ProgramFiles%\Docker\Docker\Docker Desktop.exe
    echo Start Docker Desktop manually, then reopen this file.
    echo.
    cmd /k
    exit /b 1
  )

  set DOCKER_READY=0
  for /L %%i in (1,1,60) do (
    docker info >nul 2>&1
    if not errorlevel 1 (
      set DOCKER_READY=1
      goto docker_ready
    )
    timeout /t 2 /nobreak >nul
  )

  :docker_ready
  if "%DOCKER_READY%"=="0" (
    echo.
    echo [Error] Docker Desktop did not become ready in time.
    echo Open Docker Desktop manually and wait until it says Engine running, then reopen this file.
    echo.
    cmd /k
    exit /b 1
  )
)

if "%ERPNEXT_DOCKER_NETWORK%"=="" set "ERPNEXT_DOCKER_NETWORK=frappe_docker_frappe_network"
docker network inspect "%ERPNEXT_DOCKER_NETWORK%" >nul 2>&1
if errorlevel 1 (
  echo [Error] ERPNext Docker network was not found: %ERPNEXT_DOCKER_NETWORK%
  echo Start the ERPNext sandbox first, then reopen this file.
  echo.
  cmd /k
  exit /b 1
)

echo [Docker] Starting ResolveOps services with ERPNext network access...
%COMPOSE_CMD% -f docker-compose.yml -f docker-compose.erpnext.yml up -d --build api worker
if errorlevel 1 (
  echo.
  echo [Error] Failed to start ResolveOps services.
  echo Make sure Docker Desktop is running, then try:
  echo   %COMPOSE_CMD% -f docker-compose.yml -f docker-compose.erpnext.yml up -d --build api worker
  echo.
  cmd /k
  exit /b 1
)

echo.
echo [Ready] Waiting for ResolveOps API...
set READY=0
for /L %%i in (1,1,30) do (
  python resolveops.py status >nul 2>&1
  if not errorlevel 1 (
    set READY=1
    goto ready
  )
  timeout /t 2 /nobreak >nul
)

:ready
if "%READY%"=="0" (
  echo.
  echo [Error] ResolveOps API did not become ready in time.
  echo Check service logs:
  echo   %COMPOSE_CMD% -f docker-compose.yml -f docker-compose.erpnext.yml logs --tail=100 api
  echo.
  echo If authentication failed, edit:
  echo   %USERPROFILE%\.resolveops\config.json
  echo.
  cmd /k
  exit /b 1
)

python resolveops.py status
echo.

echo Opening ResolveOps chat...
echo.
python resolveops.py chat

echo If authentication failed, edit:
echo   %USERPROFILE%\.resolveops\config.json
echo.

cmd /k

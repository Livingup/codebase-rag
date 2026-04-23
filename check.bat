@echo off
chcp 65001 >nul 2>&1

:: Resolve project root to the directory where this script lives
set "PROJECT_ROOT=%~dp0"
set "API_PORT=18080"
set "HEALTH_URL=http://127.0.0.1:%API_PORT%/api/v1/health"

:: Check health endpoint
curl -s -o nul -w "%%{http_code}" "%HEALTH_URL%" > "%TEMP%\codebase_rag_health.txt" 2>nul
set /p HTTP_CODE=<"%TEMP%\codebase_rag_health.txt"
del "%TEMP%\codebase_rag_health.txt" >nul 2>&1

if "%HTTP_CODE%"=="200" (
    :: HTTP 200 — check status field
    for /f "delims=" %%a in ('curl -s "%HEALTH_URL%" 2^>nul') do set "BODY=%%a"

    echo %BODY% | findstr /C:"unhealthy" >nul 2>&1
    if errorlevel 1 (
        echo [%date% %time%] Service is running and healthy. Skipping restart.
        goto :end
    ) else (
        echo [%date% %time%] Service is UNHEALTHY. Restarting...
    )
) else (
    echo [%date% %time%] Service is DOWN (HTTP %HTTP_CODE%). Starting...
)

:: Kill existing process
taskkill /F /FI "WINDOWTITLE eq Codebase RAG" >nul 2>&1
timeout /t 2 /nobreak >nul

:: Start service in a new window
start "Codebase RAG" cmd /c "cd /d "%PROJECT_ROOT%" && python -m codebase_rag --config "%PROJECT_ROOT%config.example.yaml" & pause"

echo [%date% %time%] Service restarted.

:end

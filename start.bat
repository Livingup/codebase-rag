@echo off
chcp 65001 >nul 2>&1
title Codebase RAG

:: Resolve project root to the directory where this script lives
set "PROJECT_ROOT=%~dp0"

echo [Codebase RAG] Stopping existing process...
taskkill /F /FI "WINDOWTITLE eq Codebase RAG" /FI "PID ne %PID%" >nul 2>&1

echo [Codebase RAG] Starting from %PROJECT_ROOT% ...
cd /d "%PROJECT_ROOT%"
python -m codebase_rag --config "%PROJECT_ROOT%config.example.yaml"

pause

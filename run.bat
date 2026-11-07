@echo off
title Micro-WMS Inbound Logistics Pipeline
cd /d "%~dp0"
echo ========================================================
echo   Micro-WMS Inbound Logistics Pipeline (Gemini 1.5)
echo ========================================================
echo.
if not exist "venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found. Initializing...
    python -m venv venv
    venv\Scripts\pip install -r requirements.txt
)
venv\Scripts\python.exe inbound_pipeline.py
pause

@echo off
title Jira Audit Report

:: ── Check Python is installed ────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo Python is not installed or not on your PATH.
    echo Please download and install Python from https://www.python.org/downloads
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

:: ── Create virtual environment on first run ──────────────────────────────────
if not exist ".venv\Scripts\activate.bat" (
    echo Setting up for the first time, please wait...
    python -m venv .venv
    if errorlevel 1 (
        echo Failed to create virtual environment.
        pause
        exit /b 1
    )
)

:: ── Activate venv ────────────────────────────────────────────────────────────
call .venv\Scripts\activate.bat

:: ── Install / update dependencies ────────────────────────────────────────────
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo Failed to install dependencies.
    pause
    exit /b 1
)

:: ── Open browser after a short delay ─────────────────────────────────────────
start "" /b cmd /c "timeout /t 2 >nul && start http://localhost:8000"

:: ── Start the server ─────────────────────────────────────────────────────────
echo.
echo Jira Audit is running at http://localhost:8000
echo Press Ctrl+C to stop.
echo.
uvicorn app:app --port 8000

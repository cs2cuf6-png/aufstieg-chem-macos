@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\cbyd.exe" (
    ".venv\Scripts\cbyd.exe" %*
) else (
    uv run cbyd %*
)

if errorlevel 1 (
    echo.
    pause
)

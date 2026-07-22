@echo off
setlocal enabledelayedexpansion
echo ===================================================
echo   GPT-Moss Agentic Client Setup (Windows)
echo ===================================================

:: 1. Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH. Please install Python 3.10+ and try again.
    pause
    exit /b 1
)

:: 2. Create Virtual Environment
if not exist "venv" (
    echo [INFO] Creating Python virtual environment in 'venv' folder...
    python -m venv venv
    if !errorlevel! neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
) else (
    echo [INFO] Virtual environment 'venv' already exists.
)

:: 3. Install dependencies
echo [INFO] Activating virtual environment and installing dependencies...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if !errorlevel! neq 0 (
    echo [ERROR] Dependency installation failed.
    pause
    exit /b 1
)

:: 4. Copy configuration templates
if not exist ".env" (
    echo [INFO] Initializing .env config file...
    copy .env.template .env >nul
)

if not exist "workspace" (
    mkdir workspace
)

if not exist "workspace\config.json" (
    echo [INFO] Initializing config.json in workspace...
    copy config.json.template workspace\config.json >nul
)

echo ===================================================
echo [SUCCESS] Setup completed successfully!
echo Run 'start.bat' to start the application.
echo ===================================================
pause

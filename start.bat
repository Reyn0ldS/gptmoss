@echo off
echo ===================================================
echo   Starting GPT-Moss Agentic Client Server
echo ===================================================

if not exist "venv" (
    echo [WARNING] Virtual environment 'venv' was not found. Running setup first...
    call install.bat
)

echo [INFO] Activating virtual environment...
call venv\Scripts\activate.bat

echo [INFO] Launching server on default port (http://127.0.0.1:8000)...
python main.py

pause

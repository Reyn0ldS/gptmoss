@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

echo ===================================================
echo   GPT-Moss Agentic Client Setup (Windows)
echo ===================================================

:: 1. Locate an existing venv, a bundled runtime, or system Python.
call "%~dp0scripts\find_python.bat"
if !errorlevel! neq 0 goto :failed

echo [INFO] Using !GPTMOSS_RUNTIME_KIND! Python: !GPTMOSS_PYTHON!
"!GPTMOSS_PYTHON!" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if !errorlevel! neq 0 (
    echo [ERROR] GPTMOSS requires Python 3.10 or newer.
    goto :failed
)

:: 2. Embedded Python is a private application runtime and cannot create venv.
if /i "!GPTMOSS_RUNTIME_KIND!"=="embedded" (
    echo [INFO] Configuring portable embedded Python...
    for %%P in ("!GPTMOSS_PYTHON!") do set "EMBEDDED_DIRECTORY=%%~dpP"
    "!GPTMOSS_PYTHON!" "%~dp0scripts\configure_embedded_python.py" --python-directory "!EMBEDDED_DIRECTORY!"
    if !errorlevel! neq 0 goto :failed

    "!GPTMOSS_PYTHON!" -c "import fastapi, httpx, openai, pydantic, uvicorn, websockets"
    if !errorlevel! neq 0 (
        echo [ERROR] The portable Python runtime does not contain all GPTMOSS dependencies.
        echo [ERROR] On an online Windows computer with the matching full Python version, run:
        echo [ERROR]   python .\scripts\prepare_portable_python.py
        goto :failed
    )
    goto :initialize
)

:: 3. Create a virtual environment for regular Python.
if /i "!GPTMOSS_RUNTIME_KIND!"=="venv" (
    echo [INFO] Virtual environment 'venv' already exists.
) else (
    "!GPTMOSS_PYTHON!" -c "import venv" >nul 2>&1
    if !errorlevel! neq 0 (
        echo [ERROR] The selected Python installation does not provide the venv module.
        echo [ERROR] Use a complete Python installation, or prepare the embedded runtime
        echo [ERROR] with .\scripts\prepare_portable_python.py on an online computer.
        goto :failed
    )
    echo [INFO] Creating Python virtual environment in 'venv' folder...
    "!GPTMOSS_PYTHON!" -m venv "%~dp0venv"
    if !errorlevel! neq 0 (
        echo [ERROR] Failed to create virtual environment.
        goto :failed
    )
)

:: 4. Prefer a local wheelhouse so installation works without network access.
echo [INFO] Installing dependencies...
if exist "%~dp0wheelhouse\*.whl" (
    echo [INFO] Installing from local offline wheelhouse...
    "%~dp0venv\Scripts\python.exe" -m pip install --no-index --find-links "%~dp0wheelhouse" -r "%~dp0requirements.txt"
) else (
    "%~dp0venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
)
if !errorlevel! neq 0 (
    echo [ERROR] Dependency installation failed.
    echo [ERROR] For an offline installation, provide a complete 'wheelhouse' directory.
    goto :failed
)

:initialize
:: 5. Copy configuration templates.
if not exist ".env" (
    echo [INFO] Initializing .env config file...
    copy ".env.template" ".env" >nul
)

if not exist "workspace" mkdir "workspace"

if not exist "workspace\config.json" (
    echo [INFO] Initializing config.json in workspace...
    copy "config.json.template" "workspace\config.json" >nul
)

echo ===================================================
echo [SUCCESS] Setup completed successfully!
echo Run 'start.bat' to start the application.
echo ===================================================
pause
exit /b 0

:failed
echo ===================================================
echo [ERROR] Setup did not complete.
echo ===================================================
pause
exit /b 1

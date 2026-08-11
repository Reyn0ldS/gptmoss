@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "PYTHONDONTWRITEBYTECODE=1"
pushd "%~dp0" >nul 2>&1
if !errorlevel! neq 0 (
    echo [ERROR] Unable to access the GPTMOSS directory: %~dp0
    exit /b 1
)

echo ===================================================
echo   Starting GPT-Moss Agentic Client Server
echo ===================================================

call "%~dp0scripts\find_python.bat"
if !errorlevel! neq 0 (
    echo [WARNING] No configured Python runtime was found. Running setup...
    call "%~dp0install.bat"
    if !errorlevel! neq 0 goto :failed
    call "%~dp0scripts\find_python.bat"
    if !errorlevel! neq 0 goto :failed
)

echo [INFO] Using !GPTMOSS_RUNTIME_KIND! Python: !GPTMOSS_PYTHON!
"!GPTMOSS_PYTHON!" -B -c "import fastapi, httpx, openai, pydantic, pytest, uvicorn, websockets"
if !errorlevel! neq 0 (
    echo [WARNING] Python dependencies are missing. Running setup...
    call "%~dp0install.bat"
    if !errorlevel! neq 0 goto :failed
    call "%~dp0scripts\find_python.bat"
    if !errorlevel! neq 0 goto :failed
)

if not defined GPTMOSS_CONTROL_PORT set "GPTMOSS_CONTROL_PORT=8765"
echo [INFO] Launching supervised server (application default: http://127.0.0.1:8000)...
echo [INFO] Server controls: http://127.0.0.1:!GPTMOSS_CONTROL_PORT!
"!GPTMOSS_PYTHON!" -B "%~dp0scripts\server_supervisor.py" --python "!GPTMOSS_PYTHON!" --main "%~dp0main.py" --control-port "!GPTMOSS_CONTROL_PORT!" -- %*
set "exit_code=!errorlevel!"

popd
pause
exit /b !exit_code!

:failed
popd
exit /b 1

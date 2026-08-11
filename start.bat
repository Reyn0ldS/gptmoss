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

echo [INFO] Launching server on default port (http://127.0.0.1:8000)...
"!GPTMOSS_PYTHON!" -B "%~dp0main.py" %*
set "exit_code=!errorlevel!"

popd
pause
exit /b !exit_code!

:failed
popd
exit /b 1

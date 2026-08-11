@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "exit_code=1"
set "pushed_directory="
set "log_file=%~dp0offline-preparation.log"
set "launcher_python="

echo ===================================================
echo   GPT-Moss Offline Runtime Preparation
echo ===================================================
echo [INFO] This maintenance tool verifies or rebuilds the bundled runtime.
echo [INFO] It does not download the GPTMOSS application sources themselves.
echo [INFO] Detailed log: %log_file%

pushd "%~dp0" >nul 2>&1
if errorlevel 1 goto :access_failed
set "pushed_directory=1"

for /d %%D in ("%~dp0python-*-embed-amd64") do if not defined launcher_python if exist "%%~fD\python.exe" set "launcher_python=%%~fD\python.exe"
if defined launcher_python goto :run_launcher

where py.exe >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 set "launcher_python=py -3"
)
if defined launcher_python goto :run_launcher

where python.exe >nul 2>&1
if not errorlevel 1 (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 set "launcher_python=python"
)
if not defined launcher_python goto :python_missing

:run_launcher
%launcher_python% -B "%~dp0scripts\prepare_offline_source_launcher.py" %*
set "exit_code=%errorlevel%"
goto :done

:access_failed
echo [ERROR] Unable to access the GPTMOSS directory: %~dp0
goto :done

:python_missing
echo [ERROR] No usable Python runtime was found.
echo [ERROR] Download the complete GPTMOSS ZIP, or install 64-bit Python 3.10+ with pip.
> "%log_file%" echo [ERROR] No usable Python runtime was found.
goto :done

:done
if defined pushed_directory popd
echo ===================================================
if "%exit_code%"=="0" (
    echo [SUCCESS] Offline runtime preparation completed.
) else (
    echo [ERROR] Offline runtime preparation failed with code %exit_code%.
    echo [ERROR] Read the diagnostic above or open: %log_file%
)
echo ===================================================
if not defined GPTMOSS_NO_PAUSE pause
exit /b %exit_code%

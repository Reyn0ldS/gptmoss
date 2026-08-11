@echo off
setlocal EnableExtensions EnableDelayedExpansion
pushd "%~dp0" >nul 2>&1
if !errorlevel! neq 0 (
    echo [ERROR] Unable to access the GPTMOSS directory: %~dp0
    exit /b 1
)

where py >nul 2>&1
if !errorlevel! equ 0 (
    py -3 "%~dp0scripts\prepare_offline_source.py" %*
    set "exit_code=!errorlevel!"
    goto :done
)

where python >nul 2>&1
if !errorlevel! equ 0 (
    python "%~dp0scripts\prepare_offline_source.py" %*
    set "exit_code=!errorlevel!"
    goto :done
)

echo [ERROR] A complete Python installation with pip is required on the online computer.
set "exit_code=1"

:done
popd
exit /b !exit_code!

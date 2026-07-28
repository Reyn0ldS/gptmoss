@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

where py >nul 2>&1
if !errorlevel! equ 0 (
    py -3 "%~dp0scripts\prepare_offline_source.py" %*
    exit /b !errorlevel!
)

where python >nul 2>&1
if !errorlevel! equ 0 (
    python "%~dp0scripts\prepare_offline_source.py" %*
    exit /b !errorlevel!
)

echo [ERROR] A complete Python installation with pip is required on the online computer.
exit /b 1

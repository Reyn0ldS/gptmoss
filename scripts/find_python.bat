@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "runtime_python="
set "runtime_kind="
set "runtime_directory="
set /a portable_count=0

if exist "%~dp0..\venv\pyvenv.cfg" if exist "%~dp0..\venv\Scripts\python.exe" (
    set "runtime_python=%~dp0..\venv\Scripts\python.exe"
    set "runtime_kind=venv"
    goto :found
)

for /d %%D in ("%~dp0..\python-*-embed-amd64") do (
    if exist "%%~fD\python.exe" (
        set /a portable_count+=1
        set "runtime_python=%%~fD\python.exe"
        set "runtime_kind=embedded"
        set "runtime_directory=%%~fD"
    )
)

if !portable_count! gtr 1 (
    echo [ERROR] Multiple portable Python runtimes were found at the project root.
    echo [ERROR] Keep only one directory matching python-*-embed-amd64.
    endlocal
    exit /b 2
)

if defined runtime_python goto :found

for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined runtime_python set "runtime_python=%%~fP"
)
if defined runtime_python (
    set "runtime_kind=system"
    goto :found
)

echo [ERROR] No usable Python runtime was found.
echo [ERROR] Install Python 3.10+ or add python-*-embed-amd64 at the project root.
endlocal
exit /b 1

:found
endlocal & (
    set "GPTMOSS_PYTHON=%runtime_python%"
    set "GPTMOSS_RUNTIME_KIND=%runtime_kind%"
    set "GPTMOSS_PYTHON_DIRECTORY=%runtime_directory%"
)
exit /b 0

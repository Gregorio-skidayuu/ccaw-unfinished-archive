@echo off
setlocal
set "CCAW_ROOT=%~dp0"
set "PYLAUNCHER_ALLOW_INSTALL="
if exist "%CCAW_ROOT%.venv\Scripts\pythonw.exe" goto run_venv
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys;sys.exit(0 if sys.version_info >= (3,11) else 2)" >nul 2>nul
    if not errorlevel 1 goto run_py
)
where python >nul 2>nul
if not errorlevel 1 (
    python -c "import sys;sys.exit(0 if sys.version_info >= (3,11) else 2)" >nul 2>nul
    if not errorlevel 1 goto run_python
)
echo CCAW requires Python 3.11+ with Tcl/Tk, or the portable CCAW.exe.
echo Download Python manually from https://www.python.org/downloads/windows/
echo No system settings have been changed.
pause
exit /b 2

:run_venv
start "" "%CCAW_ROOT%.venv\Scripts\pythonw.exe" "%CCAW_ROOT%scripts\bootstrap.py" %*
exit /b 0

:run_py
py -3 "%CCAW_ROOT%scripts\bootstrap.py" %*
exit /b %errorlevel%

:run_python
python "%CCAW_ROOT%scripts\bootstrap.py" %*
exit /b %errorlevel%

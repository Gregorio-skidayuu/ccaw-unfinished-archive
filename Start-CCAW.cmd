@echo off
setlocal DisableDelayedExpansion
set "CCAW_ROOT=%~dp0"
set "PYLAUNCHER_ALLOW_INSTALL="
if /i "%~1"=="--tk" goto find_tk
set "CCAW_DESKTOP=%CCAW_ROOT%dist\desktop\CCAW.Desktop.exe"
if not exist "%CCAW_DESKTOP%" (
    echo CCAW Desktop was not found. Extract the complete Windows package.
    echo To build from source, run scripts\build_desktop.ps1.
    echo The legacy Tk window can be opened with Start-CCAW.cmd --tk.
    exit /b 2
)

:check_overrides
if "%~1"=="" goto find_desktop_python
if /i "%~1"=="--python" goto run_desktop
shift /1
shift /1
goto check_overrides

:find_desktop_python
if exist "%CCAW_ROOT%.venv\Scripts\python.exe" goto run_desktop
set "CCAW_PYTHON_EXE="
where.exe py.exe >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%I in ('py.exe -3 -c "import sys; print(sys.executable) if sys.version_info >= (3,11) else sys.exit(2)" 2^>nul') do if not defined CCAW_PYTHON_EXE set "CCAW_PYTHON_EXE=%%I"
)
if defined CCAW_PYTHON_EXE goto run_desktop_py
where.exe python.exe >nul 2>nul
if not errorlevel 1 (
    python.exe -c "import sys;sys.exit(0 if sys.version_info >= (3,11) else 2)" >nul 2>nul
    if not errorlevel 1 goto run_desktop
)
echo CCAW Desktop requires Python 3.11 or higher.
echo Download Python from https://www.python.org/downloads/windows/
exit /b 2

:run_desktop
start "" "%CCAW_DESKTOP%" --root "%~dp0." %*
exit /b %errorlevel%

:run_desktop_py
start "" "%CCAW_DESKTOP%" --root "%~dp0." --python "%CCAW_PYTHON_EXE%" %*
exit /b %errorlevel%

:find_tk
shift /1
if exist "%CCAW_ROOT%.venv\Scripts\pythonw.exe" goto run_tk_venv
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys;sys.exit(0 if sys.version_info >= (3,11) else 2)" >nul 2>nul
    if not errorlevel 1 goto run_tk_py
)
where python >nul 2>nul
if not errorlevel 1 (
    python -c "import sys;sys.exit(0 if sys.version_info >= (3,11) else 2)" >nul 2>nul
    if not errorlevel 1 goto run_tk_python
)
echo The legacy Tk window requires Python 3.11 or higher with Tcl/Tk.
exit /b 2

:run_tk_venv
start "" "%CCAW_ROOT%.venv\Scripts\pythonw.exe" "%CCAW_ROOT%scripts\bootstrap.py" %1 %2 %3 %4 %5 %6 %7 %8 %9
exit /b %errorlevel%

:run_tk_py
py -3 "%CCAW_ROOT%scripts\bootstrap.py" %1 %2 %3 %4 %5 %6 %7 %8 %9
exit /b %errorlevel%

:run_tk_python
python "%CCAW_ROOT%scripts\bootstrap.py" %1 %2 %3 %4 %5 %6 %7 %8 %9
exit /b %errorlevel%

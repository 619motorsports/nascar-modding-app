@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title NASCAR Modding App - Native Desktop

rem One bootstrap owns both launch modes. START_LEGACY_WEB_APP.bat passes
rem --legacy; normal launches open the native Qt desktop application.
if /I "%~1"=="--legacy" goto find_python

rem Packaged releases do not require Python. Support both an extracted release
rem folder and a repository checkout containing a local PyInstaller build.
if exist "%~dp0NASCARModdingApp.exe" (
  "%~dp0NASCARModdingApp.exe"
  goto finished
)
if exist "%~dp0dist\NASCARModdingApp\NASCARModdingApp.exe" (
  "%~dp0dist\NASCARModdingApp\NASCARModdingApp.exe"
  goto finished
)

:find_python
call "%~dp0FIND_PYTHON.bat"

if not defined PYTHON_CMD if defined PYTHON_FOUND (
  echo.
  echo   Python was found, but no Python 3.10 or newer interpreter could run.
  echo   Detected versions:
  py -3 --version 2>nul
  python --version 2>nul
  python3 --version 2>nul
  echo.
  pause
  exit /b 1
)

if not defined PYTHON_CMD (
  echo.
  echo   Python 3.10 or newer was not found.
  echo   Install it from https://www.python.org/downloads/ and enable PATH.
  echo.
  pause
  exit /b 1
)

%PYTHON_CMD% -c "import flask, PIL, numpy, PySide6, OpenGL" >nul 2>nul
if errorlevel 1 (
  echo.
  echo   First run: installing the desktop dependencies...
  echo.
  %PYTHON_CMD% -m pip install -r requirements.txt
  if errorlevel 1 goto dependency_failed
  %PYTHON_CMD% -c "import flask, PIL, numpy, PySide6, OpenGL" >nul 2>nul
  if errorlevel 1 goto dependency_failed
)

if /I "%~1"=="--legacy" goto legacy

echo Starting the native desktop app...
%PYTHON_CMD% native_app.py
goto finished

:legacy
title NASCAR Modding App - Legacy Web Compatibility
echo Starting the legacy web compatibility interface...
echo Use this only for advanced workflows not yet present in the native app.
%PYTHON_CMD% app.py
goto finished

:dependency_failed
echo.
echo   Dependency installation failed. See TROUBLESHOOTING.txt.
echo.
pause
exit /b 1

:finished
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo   The app stopped with error code %RC%.
  pause
)
exit /b %RC%

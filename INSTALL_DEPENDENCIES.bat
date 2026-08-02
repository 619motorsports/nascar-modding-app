@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title NASCAR Modding App - install packages

rem You normally do not need this file: START_APP.bat installs the packages by
rem itself on first run. Use this when you want to install or repair them alone.

call "%~dp0FIND_PYTHON.bat"

if not defined PYTHON_CMD if not defined PYTHON_FOUND (
  echo.
  echo   Python was not found on this PC.
  echo.
  echo   Download it from  https://www.python.org/downloads/
  echo   and tick "Add python.exe to PATH" on the first setup screen.
  echo.
  echo   Avoid the Microsoft Store version - it cannot see this folder properly.
  echo.
  pause
  exit /b 1
)

if not defined PYTHON_CMD (
  echo   Python was found, but no Python 3.10 or newer interpreter could run.
  echo   Detected versions:
  py -3 --version 2>nul
  python --version 2>nul
  python3 --version 2>nul
  echo   Install a current version from  https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

for /f "delims=" %%V in ('%PYTHON_CMD% -c "import sys;print(str(sys.version_info[0])+chr(46)+str(sys.version_info[1]))" 2^>nul') do set "PYVER=%%V"
echo Found Python %PYVER% via "%PYTHON_CMD%".
echo.

echo Installing native desktop and legacy compatibility dependencies...
echo.
%PYTHON_CMD% -m pip install --upgrade pip
%PYTHON_CMD% -m pip install -r requirements.txt
if errorlevel 1 goto failed

%PYTHON_CMD% -c "import flask, PIL, numpy, PySide6, OpenGL" >nul 2>nul
if errorlevel 1 goto failed

echo.
echo   All required packages are installed and working.
echo   You can now run START_APP.bat.
echo.
pause
exit /b 0

:failed
echo.
echo   Installation did not finish.
echo.
echo   Most common causes:
echo     - No internet connection, or a network that blocks pip
echo     - Antivirus blocking the download
echo     - Python installed without "Add python.exe to PATH"
echo.
echo   See TROUBLESHOOTING.txt for step-by-step help.
echo.
pause
exit /b 1

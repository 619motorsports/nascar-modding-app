@echo off
rem Shared interpreter probe. Variables intentionally remain in the caller.
set "PYTHON_CMD="
set "PYTHON_FOUND="

where py >nul 2>nul
if not errorlevel 1 (
  set "PYTHON_FOUND=1"
  py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=py -3"
)

if not defined PYTHON_CMD (
  where python >nul 2>nul
  if not errorlevel 1 (
    set "PYTHON_FOUND=1"
    python -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python"
  )
)

if not defined PYTHON_CMD (
  where python3 >nul 2>nul
  if not errorlevel 1 (
    set "PYTHON_FOUND=1"
    python3 -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python3"
  )
)

exit /b 0

@echo off
setlocal
set "PYTHONUTF8=1"
cd /d "%~dp0"
if not exist pyproject.toml cd ..
if not exist pyproject.toml (
  echo Project files not found. Extract the whole project first.
  pause
  exit /b 1
)
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>nul
  if not errorlevel 1 (
    .venv\Scripts\python.exe tools\launch_workflow.py %*
    goto done
  )
)
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>nul
if not errorlevel 1 (
  py -3 tools\launch_workflow.py %*
  goto done
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>nul
if not errorlevel 1 (
  python tools\launch_workflow.py %*
  goto done
)
echo Python 3.12 or higher is required. Python 3.13 and 3.14 are allowed.
pause
exit /b 1
:done
set "workflow_exit=%errorlevel%"
pause
exit /b %workflow_exit%

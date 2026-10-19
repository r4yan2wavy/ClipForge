@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 goto folder_error

echo.
echo ClipForge - local video studio
echo.
if not exist "clipforge\__main__.py" goto folder_error
if exist ".venv\Scripts\python.exe" goto check_environment

set "CF_PYTHON="
set "CF_PY_ARGS="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "CF_PYTHON=py"
    set "CF_PY_ARGS=-3"
    goto create_environment
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "CF_PYTHON=python"
    goto create_environment
)
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "CF_PYTHON=python3"
    goto create_environment
)
echo Python 3.11 or newer was not found.
echo Install Python from https://www.python.org/downloads/ and enable Add Python to PATH.
echo Then close this window and open Start-ClipForge.bat again.
goto failed

:create_environment
echo Creating ClipForge's private Python environment...
%CF_PYTHON% %CF_PY_ARGS% -m venv ".venv"
if errorlevel 1 (
    echo Could not create .venv. Check that Python includes venv and this folder is writable.
    goto failed
)

:check_environment
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 (
    echo The existing .venv does not contain a working Python 3.11 or newer.
    echo Rename the .venv folder, install a supported Python, and start ClipForge again.
    goto failed
)
".venv\Scripts\python.exe" -c "import pydantic, requests, filelock, tzdata; assert int(pydantic.__version__.split('.')[0]) == 2" >nul 2>&1
if not errorlevel 1 goto launch
echo Installing ClipForge's basic requirements. Internet is needed for this first setup.
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -e "."
if errorlevel 1 (
    echo Setup failed. Check your internet connection and the error above, then run this launcher again.
    echo FFmpeg, Ollama, and AI models must be installed separately; they are not downloaded here.
    goto failed
)

:launch
echo Opening your browser. Keep this window open while using ClipForge.
echo Press Ctrl+C here to stop the local server.
echo.
".venv\Scripts\python.exe" -m clipforge serve %*
if errorlevel 1 (
    echo.
    echo ClipForge stopped with an error. Read the message above.
    echo If port 8000 is busy, run Start-ClipForge.bat --port 8001 from a terminal.
    goto failed
)
exit /b 0

:folder_error
echo Could not open the ClipForge project folder.
echo Extract the entire downloaded ZIP before opening this launcher.

:failed
echo.
pause
exit /b 1

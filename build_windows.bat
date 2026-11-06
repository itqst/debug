@echo off
rem Build the one-folder Windows package: build_windows.bat [output dir]
rem Default output: dist in this folder. Keep this file ASCII and CRLF: cmd reads .bat files in the system code page
rem Python: %DOG_PYTHON% if set, else the old project env, else .venv-win in this folder (created and filled on first run)
setlocal
pushd "%~dp0"
call :find_python || goto :fail
set "OUT=%~1"
if "%OUT%"=="" set "OUT=%CD%\dist"
set "WORK=%TEMP%\dog_debug_build\work"
rem The version lives only in dog_debug\__init__.py; this also writes the exe version resource
rem Read it back through a file: for /f breaks on a quoted Python path that contains parentheses
if not exist build mkdir build
"%DOG_PYTHON%" tools\make_version_file.py "%CD%\build\version_info.txt" > "%CD%\build\version.txt" || goto :fail
set /p VERSION=<"%CD%\build\version.txt"
echo Python: %DOG_PYTHON%
echo Version: %VERSION%
set "OCP_FLAG="
"%DOG_PYTHON%" -c "import OCP" 2>nul && set "OCP_FLAG=--hidden-import OCP"
echo Output: %OUT%\KunKunDebug
rem When the source is on a WSL/UNC path, pushd maps it to a temporary drive letter; the spec must be on the same drive
"%DOG_PYTHON%" -m PyInstaller --noconfirm --clean --windowed --name KunKunDebug ^
  --icon "%CD%\dog_debug\data\icon.ico" --version-file "%CD%\build\version_info.txt" ^
  --distpath "%OUT%" --workpath "%WORK%" --specpath "%CD%\build" ^
  --add-data "%CD%\dog_debug\data;dog_debug\data" ^
  --collect-submodules pyqtgraph --collect-data pyqtgraph ^
  --hidden-import OpenGL.platform.win32 --hidden-import OpenGL.arrays.numpymodule ^
  --exclude-module matplotlib --exclude-module tkinter %OCP_FLAG% ^
  "%CD%\main.py"
if errorlevel 1 goto :fail
rem The user guide has a Chinese file name; let Python glob for it
"%DOG_PYTHON%" -c "import glob,shutil,sys; [shutil.copy(f, sys.argv[1]) for f in glob.glob(r'docs\*.md') if 'STATUS' not in f]" "%OUT%\KunKunDebug"
echo.
echo Done: %OUT%\KunKunDebug\KunKunDebug.exe  (version %VERSION%)
if not defined OCP_FLAG (
  echo WARNING: cadquery-ocp is missing from this Python, so this package cannot import STEP/STP files.
  echo          It has wheels only up to Python 3.11: delete .venv-win, install Python 3.11 and run this again.
)
popd
pause
exit /b 0

:fail
echo.
echo Build FAILED, see the messages above.
popd
pause
exit /b 1

:find_python
if defined DOG_PYTHON if exist "%DOG_PYTHON%" goto :check_deps
set "DOG_PYTHON=%USERPROFILE%\.codex\runtimes\robotdog-debug-studio\Scripts\python.exe"
if exist "%DOG_PYTHON%" goto :check_deps
set "DOG_PYTHON=%CD%\.venv-win\Scripts\python.exe"
if exist "%DOG_PYTHON%" goto :check_deps
echo Creating .venv-win (Python 3.10+ needed, from python.org or "py" launcher)...
rem cadquery-ocp (STEP import) has wheels only up to Python 3.11, so prefer 3.11 / 3.10
py -3.11 -m venv .venv-win 2>nul || py -3.10 -m venv .venv-win 2>nul || py -3 -m venv .venv-win 2>nul || python -m venv .venv-win
if not exist "%DOG_PYTHON%" (
  echo Cannot create a Python environment. Install Python 3.10+ from python.org, tick "Add to PATH", then run again.
  exit /b 1
)
:check_deps
"%DOG_PYTHON%" -c "import PySide6, pyqtgraph, OpenGL, numpy, paho, yaml, paramiko, av, PyInstaller" 2>nul && exit /b 0
echo Installing dependencies into %DOG_PYTHON% ...
rem Behind a slow link to PyPI, set PIP_INDEX_URL to a mirror before running this
findstr /v /i "cadquery-ocp" requirements.txt > "%TEMP%\dog_debug_req.txt"
"%DOG_PYTHON%" -m pip install -r "%TEMP%\dog_debug_req.txt" pyinstaller==6.11.1 || (
  echo pip install failed: check the network, or set PIP_INDEX_URL to a pip mirror and run again.
  exit /b 1
)
rem STEP import is optional: without it everything else works and importing a STEP file shows a hint
"%DOG_PYTHON%" -m pip install cadquery-ocp==7.7.2 || echo cadquery-ocp not installed for this Python version: STEP import will be unavailable, continuing.
exit /b 0

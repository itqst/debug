@echo off
rem Run from source. Python: %DOG_PYTHON% if set, else the old project env, else .venv-win made by build_windows.bat
setlocal
pushd "%~dp0"
if defined DOG_PYTHON if exist "%DOG_PYTHON%" goto :run
set "DOG_PYTHON=%USERPROFILE%\.codex\runtimes\robotdog-debug-studio\Scripts\python.exe"
if exist "%DOG_PYTHON%" goto :run
set "DOG_PYTHON=%CD%\.venv-win\Scripts\python.exe"
if exist "%DOG_PYTHON%" goto :run
echo Python environment missing: run build_windows.bat once (it creates .venv-win), or set DOG_PYTHON. See README.md
pause
popd
exit /b 1
:run
"%DOG_PYTHON%" main.py %*
popd

@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
where py >nul 2>nul
if not errorlevel 1 goto use_py
where python >nul 2>nul
if not errorlevel 1 goto use_python
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" goto use_local_python
echo Python 3.10 or newer is required. Install Python or run main.py with your Python interpreter.
exit /b 2

:use_py
py -3 main.py %*
exit /b %errorlevel%

:use_python
python main.py %*
exit /b %errorlevel%

:use_local_python
"%LOCALAPPDATA%\Programs\Python\Python313\python.exe" main.py %*
exit /b %errorlevel%

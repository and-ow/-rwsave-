@echo off
rem ==========================================================
rem  .rwsave Editor - Windows launcher
rem  Pure ASCII + CRLF. Do NOT add non-ASCII chars here:
rem  cmd.exe reads this file in the OEM codepage, so Chinese
rem  text (incl. paths like the saves folder) gets split into
rem  garbage tokens. Only ASCII is safe here.
rem ==========================================================
setlocal

cd /d "%~dp0"

rem ---- Locate a Python interpreter ----
set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY goto nopython

rem ---- Ensure Flask is available ----
%PY% -c "import flask" >nul 2>nul
if not errorlevel 1 goto haveflask
echo Installing Flask...
%PY% -m pip install flask --quiet -i https://pypi.org/simple
if errorlevel 1 goto nopython
:haveflask

rem ---- Save directory: arg1 wins, else the local saves\ folder ----
set "DIR=%~1"
if not defined DIR set "DIR=%~dp0saves"
if not exist "%DIR%\" goto nodir

echo.
echo   .rwsave Editor
echo   Save dir : "%DIR%"
echo   Opening  : http://127.0.0.1:5000
echo   Stop     : close this window
echo.
%PY% rwsave_web.py --dir "%DIR%"
pause
endlocal
exit /b 0

:nopython
echo.
echo [ERROR] Python 3 not found, or Flask could not be installed.
echo         Install Python 3.8+ from https://www.python.org/downloads/
echo         and tick "Add Python to PATH", then run this file again.
echo.
pause
endlocal
exit /b 1

:nodir
echo.
echo [ERROR] Save directory not found.
echo.
echo   Create the "saves" folder next to this file and put your
echo   .rwsave files in it, OR:
echo.
echo   Usage:  drag a folder containing .rwsave files onto this .bat
echo     or:  pass the folder as the first argument, e.g.
echo           start.bat "D:\path\to\your\saves"
echo.
pause
endlocal
exit /b 1
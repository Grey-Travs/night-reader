@echo off
REM Double-click to check every chapter for mis-gendered characters.
REM Shows what it would change and asks before touching anything.
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

echo.
echo  Checking all chapters for characters referred to by the wrong gender...
echo.
"%PY%" tools\fix_pronouns.py
if errorlevel 1 goto :fail

echo.
echo  Nothing has been changed yet.
echo.
set /p ANSWER="  Apply these fixes? A backup is made first. [y/N] "
if /i not "%ANSWER%"=="y" goto :cancelled

"%PY%" tools\fix_pronouns.py --apply
echo.
echo  Done. The originals were saved in projects\_pronoun_backup_*
goto :end

:cancelled
echo.
echo  Cancelled - nothing was changed.
goto :end

:fail
echo.
echo  Something went wrong. If this is a fresh copy, run setup.bat first.

:end
echo.
pause

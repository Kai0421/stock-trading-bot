@echo off
rem Author: Kai0421
rem Windows launcher for the release layout (docs/24-hot-upgrade.md). Copy it to the layout root
rem (next to current.txt) and point the "tick" scheduled task at it:  run_current.cmd tick
rem It runs whichever release folder current.txt names, so an upgrade is one atomic pointer change.
setlocal
if "%~1"=="" (
  echo usage: run_current.cmd ^<premarket^|entries^|manage^|eod^|usage_watch^|review^|weekly_report^|weekly_review^|tick^|resume^|status^>
  exit /b 2
)
set "ROOT=%~dp0"
set "REL="
set /p REL=<"%ROOT%current.txt"
if not defined REL (
  echo no current release in %ROOT%current.txt
  exit /b 2
)
cd /d "%REL%"
if not exist logs mkdir logs
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
set PYTHONUTF8=1
uv run --frozen python -m bot.engine %1 >> "logs\%~1.log" 2>&1
exit /b %ERRORLEVEL%

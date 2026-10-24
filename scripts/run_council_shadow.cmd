@echo off
rem Author: Kai0421
rem Windows: Council of Ricks shadow log (research only, never orders). Run every 30 minutes;
rem the script decides whether anything is due (after 16:20 ET, once a day, plus labelling).
setlocal
cd /d "%~dp0.."
if not exist logs mkdir logs
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
set PYTHONUTF8=1
uv run --frozen python scripts\research\council_shadow.py run >> "logs\council_shadow.log" 2>&1
exit /b %ERRORLEVEL%

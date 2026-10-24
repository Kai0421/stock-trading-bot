@echo off
rem Author: Kai0421
rem Windows: morning health report to Slack (reads only, never orders). Weekdays 08:00.
setlocal
cd /d "%~dp0.."
if not exist logs mkdir logs
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
set PYTHONUTF8=1
uv run --frozen python scripts\health_report.py >> "logs\health_report.log" 2>&1
exit /b %ERRORLEVEL%

@echo off
rem Author: Kai0421
rem Windows version of the overnight shadow launchd job (research only, never orders).
setlocal
cd /d "%~dp0.."
if not exist logs mkdir logs
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
set PYTHONUTF8=1
uv run --frozen python scripts\research\overnight_shadow.py run >> "logs\overnight_shadow.log" 2>&1
exit /b %ERRORLEVEL%

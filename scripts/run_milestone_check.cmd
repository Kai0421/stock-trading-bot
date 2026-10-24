@echo off
rem Author: Kai0421
rem Windows: weekly note when the bot or the council reaches 30 finished trades (read-only,
rem posts once per system, never orders). Runs from the lab folder for its data and Slack settings.
setlocal
set "HERE=%~dp0.."
if not defined LAB set "LAB=%USERPROFILE%\Development\StockTradingBot-lab"
cd /d "%LAB%"
if not exist logs mkdir logs
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
set PYTHONUTF8=1
uv run --frozen --project "%HERE%" python "%HERE%\scripts\research\milestone_check.py" --live "%USERPROFILE%\Development\StockTradingBot" >> "logs\milestone_check.log" 2>&1
exit /b %ERRORLEVEL%

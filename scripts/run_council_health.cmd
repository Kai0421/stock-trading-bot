@echo off
rem Author: Kai0421
rem Windows: Council of Ricks health report (reads only, posts one Slack message to the council
rem channel, never orders). Weekdays 13:45 UK, after the council-paper run.
rem Runs from the lab folder so the council's data and Slack settings (.env) are the lab's.
setlocal
set "HERE=%~dp0.."
if not defined LAB set "LAB=%USERPROFILE%\Development\StockTradingBot-lab"
cd /d "%LAB%"
if not exist logs mkdir logs
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
set PYTHONUTF8=1
uv run --frozen --project "%HERE%" python "%HERE%\scripts\council_health.py" >> "logs\council_health.log" 2>&1
exit /b %ERRORLEVEL%

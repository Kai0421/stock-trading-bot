@echo off
rem Author: Kai0421
rem Windows: Council of Ricks practice account (pretend trades only, never orders, no broker calls).
rem Run once per weekday after the council-shadow job has recorded the day.
rem Runs from the lab folder so the Slack settings (.env) are the lab's; without them every
rem message was "not delivered". The code still comes from this folder (--project).
setlocal
set "HERE=%~dp0.."
if not defined LAB set "LAB=%USERPROFILE%\Development\StockTradingBot-lab"
cd /d "%LAB%"
if not exist logs mkdir logs
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
set PYTHONUTF8=1
uv run --frozen --project "%HERE%" python "%HERE%\scripts\research\council_paper.py" run --log "%LAB%\data\research\council_shadow.csv" --env "%LAB%\.env" --out "%LAB%\data\research" >> "logs\council_paper.log" 2>&1
exit /b %ERRORLEVEL%

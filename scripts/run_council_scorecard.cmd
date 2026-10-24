@echo off
rem Author: Kai0421
rem Windows: Council of Ricks weekly scorecard (reads the practice ledger, posts one Slack message
rem a week, never orders, no broker calls). Safe to repeat: it posts once per week.
rem Runs from the lab folder so the ledger, shadow log and Slack settings (.env) are the lab's.
setlocal
set "HERE=%~dp0.."
if not defined LAB set "LAB=%USERPROFILE%\Development\StockTradingBot-lab"
cd /d "%LAB%"
if not exist logs mkdir logs
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
set PYTHONUTF8=1
uv run --frozen --project "%HERE%" python "%HERE%\scripts\research\council_scorecard.py" --post >> "logs\council_scorecard.log" 2>&1
exit /b %ERRORLEVEL%

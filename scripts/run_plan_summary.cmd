@echo off
rem Author: Kai0421
rem Windows: post today's plan summary (what to buy and sell) to the trading-bot channel.
rem Reads the live journal read-only; runs from the lab folder for its Slack settings.
setlocal
set "HERE=%~dp0.."
if not defined LAB set "LAB=%USERPROFILE%\Development\StockTradingBot-lab"
cd /d "%LAB%"
if not exist logs mkdir logs
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
set PYTHONUTF8=1
uv run --frozen --project "%HERE%" python "%HERE%\scripts\plan_summary.py" >> "logs\plan_summary.log" 2>&1
exit /b %ERRORLEVEL%

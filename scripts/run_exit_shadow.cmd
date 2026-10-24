@echo off
rem Author: Kai0421
rem Windows: weekly read-only shadow test of a +1.5R sell target on the bot's own trades (docs/26).
rem Runs from the lab folder for its Alpaca data settings; reads the live journal, writes a report.
setlocal
set "HERE=%~dp0.."
if not defined LAB set "LAB=%USERPROFILE%\Development\StockTradingBot-lab"
cd /d "%LAB%"
if not exist logs mkdir logs
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
set PYTHONUTF8=1
uv run --frozen --project "%HERE%" python "%HERE%\scripts\research\exit_shadow.py" --live "%USERPROFILE%\Development\StockTradingBot" --env "%LAB%\.env" --notify >> "logs\exit_shadow.log" 2>&1
exit /b %ERRORLEVEL%

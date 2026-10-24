@echo off
rem Author: Kai0421
rem Windows: weekly learning loop (shadow only: reads the live journal and bars, writes reports,
rem posts one plain-English digest to Slack). Never places orders, never writes the live journal.
rem Runs from the lab folder so Slack settings come from its .env and the digest is logged there.
setlocal
set "HERE=%~dp0.."
if not defined LIVE set "LIVE=%USERPROFILE%\Development\StockTradingBot"
if not defined LAB set "LAB=%USERPROFILE%\Development\StockTradingBot-lab"
cd /d "%LAB%"
if not exist logs mkdir logs
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
set PYTHONUTF8=1
uv run --frozen --project "%HERE%" python "%HERE%\scripts\research\learning_loop.py" --journal "%LIVE%\data\bot.db" --council "%LAB%\data\research\council_paper.csv" --bars "%LIVE%\data\bars" --out "%LAB%\data\research\learning" --post >> "logs\learning_loop.log" 2>&1
exit /b %ERRORLEVEL%

@echo off
rem Author: Kai0421
rem Windows: send everything waiting in the live bot's notification queue (NOTIFY_VIA_OUTBOX).
rem Schedule it every minute only when the bot's switch is on. Runs from the lab folder for its
rem Slack settings (.env); the queue file is the live bot's.
setlocal
set "HERE=%~dp0.."
if not defined LAB set "LAB=%USERPROFILE%\Development\StockTradingBot-lab"
if not defined NOTIFIER_OUTBOX set "NOTIFIER_OUTBOX=%USERPROFILE%\Development\StockTradingBot\data\notifier_outbox.db"
cd /d "%LAB%"
if not exist logs mkdir logs
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
set PYTHONUTF8=1
uv run --frozen --project "%HERE%" python -m notifier drain >> "logs\notifier_drain.log" 2>&1
exit /b %ERRORLEVEL%

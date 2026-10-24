@echo off
rem Author: Kai0421
rem Windows: weekly safe copy of the live journal. Read-only on the source; keeps the last 8.
setlocal
if not defined LIVE set "LIVE=%USERPROFILE%\Development\StockTradingBot"
cd /d "%~dp0.."
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
set PYTHONUTF8=1
if not exist logs mkdir logs
uv run --frozen python scripts\backup_journal.py --db "%LIVE%\data\bot.db" --to "%USERPROFILE%\Development\StockTradingBot-backups" >> "logs\backup_journal.log" 2>&1
exit /b %ERRORLEVEL%

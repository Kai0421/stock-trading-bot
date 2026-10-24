@echo off
rem Author: Kai0421
rem Windows: collect Stocktwits sentiment when it is due (research only, never orders).
rem Task Scheduler runs this every 30 minutes; `bot.sentiment due` decides from New York
rem time, the market calendar and the journal whether anything is needed, so most runs end
rem here without calling Claude. Set SENTIMENT_LIMIT=5 to test with the first 5 symbols.
setlocal
cd /d "%~dp0.."
if not exist logs mkdir logs
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
set PYTHONUTF8=1
set "MODE=none"
set "HELD=-"
for /f "tokens=1,2" %%a in ('uv run --frozen python -m bot.sentiment due') do (set "MODE=%%a" & set "HELD=%%b")
if /i "%MODE%"=="none" exit /b 0
set "LIMIT=none"
if defined SENTIMENT_LIMIT set "LIMIT=%SENTIMENT_LIMIT%"
echo [%date% %time%] mode=%MODE% held=%HELD% limit=%LIMIT% >> "logs\sentiment_collect.log"
claude -p "Follow prompts/sentiment_collect.md. Mode: %MODE%. Held: %HELD%. Limit: %LIMIT%." --model claude-haiku-5-5 --permission-mode dontAsk --no-session-persistence --allowedTools "mcp__claude_ai_Stocktwits__get_sentiment,mcp__claude_ai_Stocktwits__get_message_volume,mcp__claude_ai_Stocktwits__get_symbol_pulse,Read,Edit(data/sentiment/**),Bash(uv run --frozen python -c *),Bash(uv run --frozen python -m bot.sentiment ingest *),Bash(uv run --frozen python -m bot.notify *)" >> "logs\sentiment_collect.log" 2>&1
exit /b %ERRORLEVEL%

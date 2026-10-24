@echo off
rem Author: Kai0421
rem Windows version of run_job.sh: run one engine command (a job, "tick", "resume" or "status")
rem with logs in logs\<command>.log. Used by Task Scheduler (tick every 5 minutes) and for
rem manual runs: scripts\run_job.cmd status
setlocal
if "%~1"=="" (
  echo usage: run_job.cmd ^<premarket^|entries^|manage^|eod^|usage_watch^|review^|weekly_report^|weekly_review^|tick^|resume^|status^>
  exit /b 2
)
cd /d "%~dp0.."
if not exist logs mkdir logs
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
set PYTHONUTF8=1
uv run --frozen python -m bot.engine %1 >> "logs\%~1.log" 2>&1
exit /b %ERRORLEVEL%

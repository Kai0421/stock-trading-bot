#!/bin/bash
# Author: Kai0421
# Run one engine command (a job, "tick", "resume" or "status") with logs in logs/<command>.log.
# Used by launchd (tick every 5 minutes) and for manual runs: scripts/run_job.sh eod
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
cmd="${1:?usage: run_job.sh <premarket|entries|manage|eod|usage_watch|review|weekly_report|weekly_review|tick|resume|status>}"
exec uv run --frozen python -m bot.engine "$cmd" >>"logs/$cmd.log" 2>&1

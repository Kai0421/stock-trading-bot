#!/bin/bash
# Author: Kai0421
# Claude Code status line: shows plan usage and saves a snapshot for the bot's usage alerts.
# Uses only the documented statusLine JSON fields (rate_limits.five_hour / seven_day).
set -u
input=$(cat)
state_dir="${STB_STATE_DIR:-$HOME/.local/state/stocktradingbot}"
snapshot="$state_dir/claude_usage.json"

five=$(jq -r '.rate_limits.five_hour.used_percentage // empty' <<<"$input")
week=$(jq -r '.rate_limits.seven_day.used_percentage // empty' <<<"$input")

if [[ -n "$five" || -n "$week" ]]; then
  mkdir -p "$state_dir"
  tmp=$(mktemp "$state_dir/.claude_usage.XXXXXX")
  jq -c --arg now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '{
      captured_at: $now,
      five_hour: .rate_limits.five_hour,
      seven_day: .rate_limits.seven_day
    }' <<<"$input" >"$tmp" && mv "$tmp" "$snapshot"
fi

model=$(jq -r '.model.display_name // "claude"' <<<"$input")
ctx=$(jq -r '.context_window.used_percentage // 0 | floor' <<<"$input")
printf '[%s] ctx %s%% | 5h %s%% | 7d %s%%\n' "$model" "$ctx" "${five:-?}" "${week:-?}"

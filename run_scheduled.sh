#!/bin/bash
# Fired by launchd at the tee-time release moment. Books the target tee times
# for the resident, logging everything for review in the morning.
cd /Users/mylesingram/tee-time-bot || exit 1

# Clear any stale Chrome profile lock from a previous run.
find "$HOME/.skyway_bot_profile" -maxdepth 1 -name 'Singleton*' -delete 2>/dev/null

STAMP="$(date '+%Y-%m-%d %H:%M:%S %Z')"
LOG="/Users/mylesingram/tee-time-bot/schedule.log"
{
  echo "==================================================================="
  echo "FIRED: $STAMP"
} >> "$LOG"

# caffeinate keeps the Mac awake for the duration of the booking.
caffeinate -i /Users/mylesingram/tee-time-bot/.venv/bin/python skyway_bot.py \
  --date 2026-09-27 \
  --times "09:00,11:30" \
  --players 4 \
  --headless \
  --status-file /Users/mylesingram/tee-time-bot/status.json \
  >> "$LOG" 2>&1

echo "DONE: $(date '+%Y-%m-%d %H:%M:%S %Z') (exit $?)" >> "$LOG"

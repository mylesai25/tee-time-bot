#!/bin/bash
# Fired by launchd at 00:00 on each configured day. At that moment the day
# exactly 7 days out has just opened for booking, so we target it. The tee
# times and party size come from config.json (edited via the web panel).
DIR=/Users/mylesingram/tee-time-bot
cd "$DIR" || exit 1
PY="$DIR/.venv/bin/python"
CFG="$DIR/config.json"

find "$HOME/.skyway_bot_profile" -maxdepth 1 -name 'Singleton*' -delete 2>/dev/null

DATE=$(date -v+7d +%Y-%m-%d)                                    # the day that just released
TIMES=$("$PY" -c "import json;print(','.join(json.load(open('$CFG'))['times']))")
PLAYERS=$("$PY" -c "import json;print(json.load(open('$CFG'))['players'])")

LOG="$DIR/schedule.log"
{
  echo "==================================================================="
  echo "FIRED: $(date '+%Y-%m-%d %H:%M:%S %Z')  (target $DATE, times $TIMES, ${PLAYERS} players)"
} >> "$LOG"

# caffeinate keeps the Mac awake for the duration of the booking.
caffeinate -i "$PY" skyway_bot.py \
  --date "$DATE" \
  --times "$TIMES" \
  --players "$PLAYERS" \
  --headless \
  --status-file "$DIR/status.json" \
  >> "$LOG" 2>&1

echo "DONE: $(date '+%Y-%m-%d %H:%M:%S %Z') (exit $?)" >> "$LOG"

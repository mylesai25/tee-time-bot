#!/bin/bash
# Fired by launchd a couple of minutes BEFORE midnight on the night before each
# configured day (see server.py). The day exactly 7 days after the coming
# midnight opens for booking at 00:00, so we sign in and pre-warm the booking
# widget now, then book the instant the sheet appears. If launchd fires us at
# or after midnight instead (old schedule / late wake), we book straight away.
DIR=/Users/mylesingram/tee-time-bot
cd "$DIR" || exit 1
PY="$DIR/.venv/bin/python"
CFG="$DIR/config.json"

find "$HOME/.skyway_bot_profile" -maxdepth 1 -name 'Singleton*' -delete 2>/dev/null

if [ "$(date +%H)" = "23" ]; then
  DATE=$(date -v+8d +%Y-%m-%d)          # tomorrow + 7: the day that opens at midnight
  RELEASE=(--release-at 00:00)
else
  DATE=$(date -v+7d +%Y-%m-%d)          # already past midnight: the day just released
  RELEASE=()
fi
TIMES=$("$PY" -c "import json;print(','.join(json.load(open('$CFG'))['times']))")
PLAYERS=$("$PY" -c "import json;print(json.load(open('$CFG'))['players'])")

LOG="$DIR/schedule.log"
{
  echo "==================================================================="
  echo "FIRED: $(date '+%Y-%m-%d %H:%M:%S %Z')  (target $DATE, times $TIMES, ${PLAYERS} players${RELEASE:+, pre-warm for 00:00})"
} >> "$LOG"

# Independent watcher: logs when the public API first shows the day and how
# fast slots vanish, so we can see when the sheet really opens.
"$PY" probe_release.py --date "$DATE" --players "$PLAYERS" --release-at 00:00 \
  >> "$DIR/release_probe.log" 2>&1 &

# caffeinate keeps the Mac awake for the duration of the booking.
caffeinate -i "$PY" skyway_bot.py \
  --date "$DATE" \
  --times "$TIMES" \
  --players "$PLAYERS" \
  "${RELEASE[@]}" \
  --headless \
  --status-file "$DIR/status.json" \
  >> "$LOG" 2>&1

echo "DONE: $(date '+%Y-%m-%d %H:%M:%S %Z') (exit $?)" >> "$LOG"

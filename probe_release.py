#!/usr/bin/env python3
"""
Release-time probe: watches the public tee-time API for a date and logs the
moment it first appears, plus how quickly foursome slots disappear afterwards.

Runs alongside the booking bot (no browser needed) so we learn *when*
Chronogolf actually opens the sheet and how fast the competition is.

    python probe_release.py --date 2026-10-04 --players 4 --release-at 00:00
"""
import argparse
import json
import re
import time
import urllib.request
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

COURSE_ID = "0b833d14-8c0d-46ca-82e6-7b992de4761e"
API = "https://www.chronogolf.com/marketplace/v2/teetimes"


def fetch(date_str, players, holes=9):
    url = (f"{API}?start_date={date_str}&free_slots={players}"
           f"&course_ids={COURSE_ID}&holes={holes}&page=1")
    req = urllib.request.Request(url, headers={"accept": "application/json",
                                               "user-agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        server_date = r.headers.get("Date")
        data = json.loads(r.read().decode())
    slots = data.get("teetimes") if isinstance(data, dict) else []
    times = []
    for s in slots or []:
        m = re.search(r"T(\d{2}):(\d{2})", str(s.get("start_time", ""))) or \
            re.match(r"\s*(\d{1,2}):(\d{2})", str(s.get("start_time", "")))
        if m and not s.get("out_of_capacity") and not s.get("frozen"):
            times.append(f"{int(m.group(1))%12 or 12}:{m.group(2)}{'a' if int(m.group(1))<12 else 'p'}")
    return times, server_date


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--players", type=int, default=4)
    ap.add_argument("--release-at", default="00:00")
    ap.add_argument("--after", type=int, default=180, help="seconds to keep watching after release")
    a = ap.parse_args()
    now = datetime.now()
    rel = now.replace(hour=int(a.release_at[:2]), minute=int(a.release_at[3:]), second=0, microsecond=0)
    if rel < now - timedelta(minutes=10):
        rel += timedelta(days=1)
    stop = rel + timedelta(seconds=a.after)
    print(f"=== probe {a.date} for {a.players}: started {now:%Y-%m-%d %H:%M:%S}, release {rel:%H:%M:%S}", flush=True)

    last = None
    last_count = None      # slot count of the previous successful poll
    first_seen = None
    checked_clock = False
    while datetime.now() < stop:
        t0 = datetime.now()
        try:
            times, server_date = fetch(a.date, a.players)
            state = f"{len(times)} open" + (f": {' '.join(times[:12])}" + (" ..." if len(times) > 12 else "") if times else "")
        except Exception as e:
            times, server_date, state = None, None, f"error {type(e).__name__}: {str(e)[:60]}"
        rel_s = (t0 - rel).total_seconds()
        if not checked_clock and server_date:
            try:
                srv = parsedate_to_datetime(server_date).astimezone().replace(tzinfo=None)
                print(f"[{t0:%H:%M:%S.%f}] clock check: server says {srv:%H:%M:%S}, local {t0:%H:%M:%S} "
                      f"(local - server = {(t0 - srv).total_seconds():+.0f}s, 1s resolution)", flush=True)
            except Exception:
                pass
            checked_clock = True
        if times and first_seen is None and last_count == 0:
            first_seen = t0
            print(f"[{t0:%H:%M:%S.%f}] *** RELEASE SEEN at T{rel_s:+.2f}s", flush=True)
        if state != last:
            print(f"[{t0:%H:%M:%S.%f}] T{rel_s:+.1f}s  {state}", flush=True)
            last = state
        if times is not None:
            last_count = len(times)
        # The public API answers 429 after ~20 requests in ~20s, so stay well
        # under that: 2s cadence around release, 5s elsewhere, 10s after a 429.
        if times is None and "429" in state:
            time.sleep(10)
        else:
            time.sleep(2.0 if -10 <= rel_s <= 30 else 5.0)
    print(f"=== probe done {datetime.now():%H:%M:%S}", flush=True)


if __name__ == "__main__":
    main()

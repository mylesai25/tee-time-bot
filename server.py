#!/usr/bin/env python3
"""
Config panel for the Skyway weekend tee-time bot.

Serves a small web UI to edit config.json (which days to book, the wake time,
the ideal tee times, and party size). On save it regenerates the launchd jobs
so the schedule matches, and returns the one `sudo pmset` command you need to
run to update the wake (that step needs admin rights, which the server can't).

Run:  python server.py     # then open http://localhost:8000
Uses only the Python standard library.
"""
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
INDEX = HERE / "index.html"
CONFIG = HERE / "config.json"
WRAPPER = HERE / "run_scheduled.sh"
AGENTS = Path.home() / "Library" / "LaunchAgents"
BOOK_PLIST = AGENTS / "com.skyway.teetimebot.plist"
WAKE_PLIST = AGENTS / "com.skyway.stayawake.plist"
PORT = int(os.environ.get("PORT", "8000"))

DAYS_ORDER = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
              "Friday", "Saturday"]
WD = {name: i for i, name in enumerate(DAYS_ORDER)}          # Sunday=0 .. Saturday=6
PMSET_CODE = {0: "U", 1: "M", 2: "T", 3: "W", 4: "R", 5: "F", 6: "S"}
DEFAULT = {"days": ["Saturday", "Sunday"], "wake_time": "23:52",
           "times": ["07:00", "10:30"], "players": 4}


PREWARM_AFTER_WAKE_MIN = 3   # bot fires this many minutes after the Mac wakes (23:55 -> 23:58)


def night_before(weekday):
    return (weekday + 6) % 7


def read_config():
    try:
        cfg = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        cfg = dict(DEFAULT)
    return cfg


def validate(cfg):
    days = [d for d in cfg.get("days", []) if d in WD]
    if not days:
        return None, "Pick at least one day."
    wake = str(cfg.get("wake_time", "")).strip()
    import re
    if not re.match(r"^\d{2}:\d{2}$", wake):
        return None, "Wake time must be HH:MM."
    wh, wm = int(wake[:2]), int(wake[3:])
    if not (0 <= wh < 24 and 0 <= wm < 60) or wh < 22:
        return None, "Wake time must be a late-evening time (22:00–23:59), before the midnight booking."
    times = [t.strip() for t in cfg.get("times", []) if str(t).strip()]
    if not times or any(not re.match(r"^\d{2}:\d{2}$", t) for t in times):
        return None, "Each tee time must be HH:MM."
    try:
        players = int(cfg.get("players", 4))
    except (TypeError, ValueError):
        return None, "Players must be a number."
    if players not in (1, 2, 3, 4):
        return None, "Players must be 1–4."
    return {"days": days, "wake_time": wake, "times": times, "players": players}, None


# ---------- launchd plist generation ----------

def _intervals(entries):
    out = []
    for wd, h, m in entries:
        out.append(
            "        <dict>\n"
            f"            <key>Weekday</key><integer>{wd}</integer>\n"
            f"            <key>Hour</key><integer>{h}</integer>\n"
            f"            <key>Minute</key><integer>{m}</integer>\n"
            "        </dict>")
    return "\n".join(out)


def _plist(label, program_args, entries):
    args_xml = "\n".join(f"        <string>{a}</string>" for a in program_args)
    logs = ""
    if label == "com.skyway.teetimebot":
        logs = (f"    <key>StandardOutPath</key><string>{HERE}/launchd.out.log</string>\n"
                f"    <key>StandardErrorPath</key><string>{HERE}/launchd.err.log</string>\n")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f"    <key>Label</key><string>{label}</string>\n"
        "    <key>ProgramArguments</key>\n    <array>\n" + args_xml + "\n    </array>\n"
        "    <key>StartCalendarInterval</key>\n    <array>\n"
        + _intervals(entries) + "\n    </array>\n" + logs +
        "</dict>\n</plist>\n")


def add_minutes(h, m, delta):
    total = (h * 60 + m + delta) % (24 * 60)
    return total // 60, total % 60


def apply_config(cfg):
    """Write config, regenerate + reload launchd jobs. Returns the pmset command."""
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")

    book_wds = [WD[d] for d in cfg["days"]]
    wh, wm = int(cfg["wake_time"][:2]), int(cfg["wake_time"][3:])
    ch, cm = add_minutes(wh, wm, 2)                   # caffeinate 2 min after wake
    wake_wds = [night_before(w) for w in book_wds]    # wake the night before each booking

    # The bot fires a few minutes before midnight on the night before, signs in
    # and pre-warms the booking widget, then books the instant the sheet opens
    # at 00:00 (run_scheduled.sh handles the date arithmetic). Fall back to
    # firing at 00:00 if the wake time is so late that there's no room.
    bh, bm = add_minutes(wh, wm, PREWARM_AFTER_WAKE_MIN)
    if (bh, bm) > (wh, wm):                           # still before midnight
        book_entries = [(night_before(w), bh, bm) for w in book_wds]
    else:
        book_entries = [(w, 0, 0) for w in book_wds]
    BOOK_PLIST.write_text(_plist(
        "com.skyway.teetimebot", ["/bin/bash", str(WRAPPER)], book_entries))
    WAKE_PLIST.write_text(_plist(
        "com.skyway.stayawake", ["/usr/bin/caffeinate", "-dimsu", "-t", "2400"],
        [(w, ch, cm) for w in wake_wds]))             # caffeinate the night before

    for plist in (BOOK_PLIST, WAKE_PLIST):
        subprocess.run(["launchctl", "unload", str(plist)],
                       capture_output=True)
        subprocess.run(["launchctl", "load", str(plist)], capture_output=True)

    codes = "".join(PMSET_CODE[w] for w in sorted(set(wake_wds)))
    return f"sudo pmset repeat wake {codes} {cfg['wake_time']}:00"


def summary(cfg):
    book_wds = sorted({WD[d] for d in cfg["days"]})
    wake_days = [DAYS_ORDER[night_before(w)] for w in book_wds]
    return {
        "book_days": [DAYS_ORDER[w] for w in book_wds],
        "wake_days": wake_days,
        "wake_time": cfg["wake_time"],
        "fire_time": "%02d:%02d" % add_minutes(int(cfg["wake_time"][:2]),
                                               int(cfg["wake_time"][3:]),
                                               PREWARM_AFTER_WAKE_MIN),
        "pmset_cmd": f"sudo pmset repeat wake "
                     f"{''.join(PMSET_CODE[night_before(w)] for w in book_wds)} "
                     f"{cfg['wake_time']}:00",
    }


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                self._send(200, INDEX.read_text(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, "index.html not found", "text/plain")
        elif self.path == "/api/config":
            cfg = read_config()
            self._send(200, {"config": cfg, "summary": summary(cfg)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/config":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            raw = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send(400, {"ok": False, "message": "bad JSON"})
            return
        cfg, err = validate(raw)
        if err:
            self._send(400, {"ok": False, "message": err})
            return
        pmset_cmd = apply_config(cfg)
        self._send(200, {"ok": True, "message": "Saved and schedule updated.",
                         "config": cfg, "summary": summary(cfg),
                         "pmset_cmd": pmset_cmd})

    def log_message(self, *args):
        pass


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Skyway config panel: http://localhost:{PORT}")
    print("Ctrl+C to stop (the scheduled jobs keep running).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()

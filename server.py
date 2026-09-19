#!/usr/bin/env python3
"""
Control-panel backend for the Skyway tee time bot.

Serves the web frontend (index.html) and a small JSON API to turn the bot on
and off and read its live status. Uses only the Python standard library, so
there is nothing extra to install for the panel itself (the bot still needs
Playwright per the README).

Run
    python server.py            # then open http://localhost:8000

The bot writes its progress to status.json; this server starts/stops the bot
as a subprocess and reports whether it is currently running.
"""
import json
import os
import re
import signal
import subprocess
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOT = HERE / "skyway_bot.py"
# Run the bot with the project venv's Python (where Playwright is installed),
# falling back to whatever Python is running this server.
_VENV_PY = HERE / ".venv" / "bin" / "python"
BOT_PYTHON = str(_VENV_PY) if _VENV_PY.exists() else sys.executable
INDEX = HERE / "index.html"
STATUS_FILE = HERE / "status.json"
PID_FILE = HERE / "bot.pid"

VALID_TIME = re.compile(r"^\d{2}:\d{2}$")


def load_env_file(path=HERE / ".env"):
    """Load KEY=VALUE lines from a local .env into the environment, if present.

    Keeps personal config (email addresses, SMTP password) out of the source
    tree. Existing environment variables always win. The bot subprocess
    inherits these, so credentials set here reach it too.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()
PORT = int(os.environ.get("PORT", "8000"))


# ---------- process management ----------

def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def running_pid():
    """Return the PID of a live bot process, or None."""
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None
    return pid if _pid_alive(pid) else None


def start_bot(cfg):
    """Launch the bot with the given config. Returns (ok, message)."""
    if running_pid():
        return False, "Bot is already running."

    date = str(cfg.get("date", "")).strip()
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return False, "Date must be YYYY-MM-DD."

    earliest = str(cfg.get("earliest", "06:00")).strip()
    latest = str(cfg.get("latest", "10:00")).strip()
    if not VALID_TIME.match(earliest) or not VALID_TIME.match(latest):
        return False, "Times must be HH:MM (24-hour)."

    try:
        players = int(cfg.get("players", 2))
    except (TypeError, ValueError):
        return False, "Players must be a number."
    if players not in (1, 2, 3, 4):
        return False, "Players must be 1-4."

    cmd = [BOT_PYTHON, str(BOT),
           "--date", date,
           "--earliest", earliest,
           "--latest", latest,
           "--players", str(players),
           "--headless",
           "--status-file", str(STATUS_FILE)]

    notify_email = (str(cfg.get("notify_email", "")).strip()
                    or os.environ.get("SKYWAY_NOTIFY_EMAIL", "").strip())
    if notify_email:
        cmd += ["--notify-email", notify_email]

    start_at = str(cfg.get("start_at", "")).strip()
    if start_at:
        if not VALID_TIME.match(start_at):
            return False, "Start-at time must be HH:MM (24-hour)."
        cmd += ["--start-at", start_at]

    try:
        poll = int(cfg.get("poll", 60))
        cmd += ["--poll", str(poll)]
    except (TypeError, ValueError):
        pass

    if cfg.get("dry_run"):
        cmd.append("--dry-run")

    # Fresh status so the panel doesn't show a stale "booked" from last time.
    STATUS_FILE.write_text(json.dumps({
        "state": "starting", "message": "Launching bot...",
        "date": date, "earliest": earliest, "latest": latest, "players": players,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }, indent=2))

    proc = subprocess.Popen(cmd, cwd=str(HERE))
    PID_FILE.write_text(str(proc.pid))
    return True, f"Bot started (pid {proc.pid})."


def stop_bot():
    """Stop a running bot. Returns (ok, message)."""
    pid = running_pid()
    if not pid:
        PID_FILE.unlink(missing_ok=True)
        return False, "Bot is not running."
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        return False, f"Could not stop bot: {e}"
    PID_FILE.unlink(missing_ok=True)

    # Reflect the stop in status.json unless the bot already finished/booked.
    try:
        status = json.loads(STATUS_FILE.read_text())
    except (OSError, ValueError):
        status = {}
    if status.get("state") not in ("booked", "gave_up"):
        status.update(state="stopped", message="Bot stopped.",
                      updated_at=datetime.now().isoformat(timespec="seconds"))
        STATUS_FILE.write_text(json.dumps(status, indent=2))
    return True, "Bot stopped."


def read_status():
    try:
        status = json.loads(STATUS_FILE.read_text())
    except (OSError, ValueError):
        status = {"state": "idle", "message": "Bot has not been started yet."}
    status["running"] = running_pid() is not None
    return status


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

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                self._send(200, INDEX.read_text(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, "index.html not found", "text/plain")
        elif self.path == "/api/status":
            self._send(200, read_status())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/start":
            ok, msg = start_bot(self._read_json())
            self._send(200 if ok else 400, {"ok": ok, "message": msg})
        elif self.path == "/api/stop":
            ok, msg = stop_bot()
            self._send(200 if ok else 400, {"ok": ok, "message": msg})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, *args):  # quieter console
        pass


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Skyway bot control panel: http://localhost:{PORT}")
    print("Press Ctrl+C to stop the panel (the bot keeps running if started).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down panel.")
        server.shutdown()


if __name__ == "__main__":
    main()

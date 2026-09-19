#!/usr/bin/env python3
"""
Skyway Golf Course tee time bot (Chronogolf / Lightspeed).

Watches Skyway's Chronogolf page for a tee time in your window and reserves
the earliest match using your own logged-in Chronogolf account. Skyway is
"reserve now, pay later", so no card details are involved.

Setup
    pip install playwright
    playwright install chromium
    python skyway_bot.py --login          # one time: log in by hand, session is saved

Use
    # Look, don't book (do this first, and watch it work):
    python skyway_bot.py --date 2026-09-26 --earliest 07:00 --latest 09:30 --players 2 --dry-run

    # Watch for cancellations and grab the first match:
    python skyway_bot.py --date 2026-09-26 --earliest 07:00 --latest 09:30 --players 2

    # Sit idle until the booking window opens (local time), then go:
    python skyway_bot.py --date 2026-09-26 --earliest 07:00 --latest 09:30 --players 4 --start-at 19:00

How it works
    Availability is read from the JSON the page itself fetches (any response
    with "teetime" in the URL), so it doesn't depend on page layout. Booking is
    done by clicking through the UI; every step is screenshotted to
    ./skyway_bot_screens/ so you can see exactly where it got to.
"""
import argparse
import json
import os
import random
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

CLUB_URL = "https://www.chronogolf.com/club/skyway-golf-course"
PROFILE_DIR = Path.home() / ".skyway_bot_profile"   # saved browser session (your login)
SHOTS = Path("skyway_bot_screens")
MIN_POLL_SECONDS = 20                                # be a polite guest

# Path the frontend reads for live status; set from --status-file at startup.
STATUS_FILE = None
_status = {}


def emit(**fields):
    """Merge fields into the running status and write them atomically as JSON.

    A no-op unless --status-file was passed, so the CLI is unaffected.
    """
    if not STATUS_FILE:
        return
    _status.update(fields)
    _status["updated_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_status, indent=2))
    tmp.replace(STATUS_FILE)


def send_confirmation_email(to_addr, subject, body):
    """Email a booking confirmation via SMTP. Returns (ok, detail).

    Credentials come from the environment so nothing sensitive lives in code:
        SKYWAY_SMTP_USER   sending address (e.g. you@gmail.com)
        SKYWAY_SMTP_PASS   app password (Gmail: an App Password, not your login)
        SKYWAY_SMTP_HOST   default smtp.gmail.com
        SKYWAY_SMTP_PORT   default 587 (STARTTLS)
    """
    user = os.environ.get("SKYWAY_SMTP_USER")
    password = os.environ.get("SKYWAY_SMTP_PASS")
    if not to_addr or not user or not password:
        return False, ("email not configured (set SKYWAY_SMTP_USER, "
                       "SKYWAY_SMTP_PASS and a recipient)")

    host = os.environ.get("SKYWAY_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SKYWAY_SMTP_PORT", "587"))

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(user, password)
            smtp.send_message(msg)
        return True, f"sent to {to_addr}"
    except Exception as e:  # never let a mail failure crash a successful booking
        return False, f"{type(e).__name__}: {e}"


def load_env_file(path=Path(__file__).with_name(".env")):
    """Load KEY=VALUE lines from a local .env into the environment, if present.

    Keeps personal config (email addresses, SMTP password) out of the source
    tree. Existing environment variables always win.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# Buttons that move the booking flow forward, and text that means we're done.
ADVANCE = re.compile(
    r"^\s*(continue|next|proceed|book now|reserve|confirm|complete)\b", re.I)
FINAL = re.compile(r"reserve|confirm|complete", re.I)
DONE = re.compile(
    r"reservation (is |has been )?confirmed|booking (is )?confirmed|confirmation (number|#)",
    re.I)


def build_url(date_str: str, players: int) -> str:
    # If Chronogolf changes its query params, this is the one line to fix.
    return f"{CLUB_URL}?date={date_str}&step=teetimes&holes=9&groupSize={players}"


# ---------- availability ----------

def find_slots(node):
    """Walk arbitrary JSON and yield every dict that looks like a tee time."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if "start_time" in cur:
                yield cur
            else:
                stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def slot_minutes(slot):
    """start_time -> minutes after midnight. Handles '7:30', '07:30', ISO strings."""
    raw = str(slot.get("start_time", ""))
    m = re.search(r"T(\d{2}):(\d{2})", raw) or re.match(r"\s*(\d{1,2}):(\d{2})", raw)
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def slot_fits(slot, players):
    if slot.get("out_of_capacity") or slot.get("frozen"):
        return False
    hi, lo = slot.get("max_player_size"), slot.get("min_player_size")
    if isinstance(hi, int) and hi < players:
        return False
    if isinstance(lo, int) and lo > players:
        return False
    return True


def label(minutes):
    """450 -> '7:30 AM' (how the site displays it)."""
    h, m = divmod(minutes, 60)
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def hhmm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def check(page, args):
    """Load the tee sheet, return sorted minutes-after-midnight of matching slots."""
    responses = []

    def grab(resp):
        if "teetime" in resp.url.lower():
            responses.append(resp)

    page.on("response", grab)
    try:
        page.goto(build_url(args.date, args.players), wait_until="domcontentloaded")
        deadline = time.time() + 20
        while not responses and time.time() < deadline:
            page.wait_for_timeout(500)
        page.wait_for_timeout(1500)  # let any follow-up requests land
    finally:
        page.remove_listener("response", grab)

    slots = []
    for resp in responses:
        try:
            slots.extend(find_slots(resp.json()))
        except Exception:
            continue

    if not responses:
        print("  ! No tee time request seen. Run without --headless and look at the page;"
              " build_url() may need updating.")
        return []

    dated = [s for s in slots if s.get("date")]
    if dated and not any(str(s["date"]).startswith(args.date) for s in dated):
        print(f"  ! Page returned tee times for {dated[0]['date']}, not {args.date}."
              " The date isn't bookable yet, or build_url() needs updating.")
        return []

    lo, hi = hhmm(args.earliest), hhmm(args.latest)
    hits = set()
    for s in slots:
        if s.get("date") and not str(s["date"]).startswith(args.date):
            continue
        mins = slot_minutes(s)
        if mins is not None and lo <= mins <= hi and slot_fits(s, args.players):
            hits.add(mins)
    return sorted(hits)


# ---------- browser ----------

def pause(page, lo_ms, hi_ms):
    """Wait a random human-like interval (avoids robotic fixed-timing)."""
    page.wait_for_timeout(random.randint(lo_ms, hi_ms))


def nap_seconds(poll):
    """Jittered gap between checks so polling isn't a metronome. Honors floor."""
    return max(MIN_POLL_SECONDS, random.uniform(poll * 0.75, poll * 1.4))


# Chrome flags + JS shim that hide the tell-tale signs of automation Cloudflare
# fingerprints (navigator.webdriver, the "Chrome is being controlled..." bar).
STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]
STEALTH_JS = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"


def launch_context(pw, args, headless):
    """Open a persistent browser context, preferring the real system Chrome.

    Cloudflare blocks Playwright's bundled Chromium (especially the headless
    shell). Driving the installed Google Chrome with automation flags stripped
    behaves like a normal browser, so its challenge can be solved by hand.
    """
    opts = dict(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1280, "height": 900},
        args=STEALTH_ARGS,
        ignore_default_args=["--enable-automation"],
    )
    if args.channel is None:
        channels = ["chrome", None]   # auto: real Chrome, then bundled Chromium
    elif args.channel == "":
        channels = [None]             # force Playwright's bundled Chromium
    else:
        channels = [args.channel]     # a specific channel, e.g. chrome / msedge
    last_err = None
    for ch in channels:
        try:
            ctx = pw.chromium.launch_persistent_context(channel=ch, **opts) if ch \
                else pw.chromium.launch_persistent_context(**opts)
            if ch:
                print(f"Using browser channel: {ch}")
            ctx.add_init_script(STEALTH_JS)
            return ctx
        except Exception as e:  # channel not installed -> fall back to next
            last_err = e
            print(f"  ! Could not launch channel {ch!r}: {type(e).__name__}")
    raise last_err


LOGIN_URL = "https://www.chronogolf.com/login"


def is_logged_in(page, timeout_ms=12000):
    """True if a signed-in account shows up within the timeout.

    Polls, because Chronogolf renders its header client-side and 'My Account'
    can take a moment to appear after navigation.
    """
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        body = page.inner_text("body") if page.query_selector("body") else ""
        if "My Account" in body or "My account" in body:
            return True
        if "Log In" in body or "Log in" in body:  # header rendered, logged out
            return False
        page.wait_for_timeout(500)
    return False


def _attempt_login(page, user, password):
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)
    page.fill("#sessionEmail", user)
    page.fill("#sessionPassword", password)

    # Let Cloudflare Turnstile populate its token. It auto-solves for real
    # Chrome; if it escalates to a checkbox, this window also lets a human click
    # it (headed/assisted runs).
    for _ in range(90):
        has_token = page.evaluate(
            """() => Array.from(
                   document.querySelectorAll('input[name=\"cf-turnstile-response\"]'))
                   .some(e => e.value && e.value.length > 20)""")
        if has_token:
            break
        page.wait_for_timeout(1000)

    page.click("input[type=submit]")
    # Wait for the login page to hand off (URL leaves /login) or an error.
    for _ in range(40):
        page.wait_for_timeout(1000)
        if "/login" not in page.url:
            break

    # Confirm on the club page, giving the SPA time to render the header.
    page.goto(CLUB_URL, wait_until="domcontentloaded", timeout=45000)
    return is_logged_in(page, timeout_ms=15000)


def ensure_logged_in(page, tries=1):
    """Make sure we're signed in, logging in with env credentials if needed.

    The saved browser session doesn't survive a restart, so each run signs in
    afresh using CHRONO_USER / CHRONO_PASS from the environment (.env).
    Retries because Turnstile/redirect timing is occasionally flaky.
    """
    page.goto(CLUB_URL, wait_until="domcontentloaded", timeout=45000)
    if is_logged_in(page):
        return True

    user, password = os.environ.get("CHRONO_USER"), os.environ.get("CHRONO_PASS")
    if not user or not password:
        print("  ! Not logged in and CHRONO_USER/CHRONO_PASS are not set.")
        return False

    for attempt in range(1, tries + 1):
        print(f"  Signing in to Chronogolf (attempt {attempt}/{tries})...")
        if _attempt_login(page, user, password):
            return True
    return False


# ---------- booking ----------

def shot(page, name):
    SHOTS.mkdir(exist_ok=True)
    path = SHOTS / f"{datetime.now():%H%M%S}_{name}.png"
    page.screenshot(path=str(path), full_page=True)
    return path


def book(page, minutes, args):
    """Click the slot, then click through until confirmed. Returns True if booked."""
    text = label(minutes)
    page.get_by_text(re.compile(rf"^\s*{re.escape(text)}\s*$", re.I)).first.click(timeout=10000)
    pause(page, 1100, 2200)
    shot(page, "1_slot_clicked")

    # Player count picker, if the flow shows one.
    picker = page.get_by_role("button", name=re.compile(rf"^\s*{args.players}\s*$")).first
    if picker.is_visible():
        picker.click()
        pause(page, 600, 1400)

    for step in range(2, 10):
        if page.get_by_text(DONE).first.is_visible():
            path = shot(page, 'confirmed')
            print(f"  Booked {text}. Screenshot: {path}")
            booked_at = datetime.now().isoformat(timespec="seconds")
            emit(state="booked",
                 message=f"Booked {text} on {args.date} for {args.players}.",
                 booked_time=text, booked_date=args.date,
                 booked_at=booked_at, screenshot=str(path))

            if args.notify_email:
                subject = f"⛳ Tee time booked: {text} on {args.date}"
                body = (f"Your Skyway tee time is booked.\n\n"
                        f"  Time:    {text}\n"
                        f"  Date:    {args.date}\n"
                        f"  Players: {args.players}\n"
                        f"  Booked:  {booked_at.replace('T', ' ')}\n\n"
                        f"Screenshot saved to: {path}\n")
                ok, detail = send_confirmation_email(args.notify_email, subject, body)
                print(f"  Email confirmation: {detail}")
                emit(email_sent=ok, email_detail=detail)
            return True

        # Tick any terms / policy checkboxes.
        for box in page.get_by_role("checkbox").all():
            if box.is_visible() and not box.is_checked():
                box.check()

        btn = page.get_by_role("button", name=ADVANCE).first
        try:
            btn.wait_for(state="visible", timeout=10000)
        except PWTimeout:
            break
        name = btn.inner_text().strip()
        if args.dry_run and FINAL.search(name):
            print(f"  [dry run] Stopped before clicking '{name}' for {text}."
                  f" Screenshot: {shot(page, 'dry_run_stop')}")
            return False
        # A brief "reading the page" beat before advancing, then the click.
        pause(page, 500, 1500)
        btn.click()
        pause(page, 1400, 2800)
        slug = re.sub(r"\W+", "_", name.lower())[:20]
        shot(page, f"{step}_{slug}")

    print(f"  ! Got stuck booking {text}. See {shot(page, 'stuck')} - the button labels in"
          " ADVANCE / DONE probably need adjusting.")
    return False


# ---------- main ----------

def wait_until(start_at):
    target = datetime.now().replace(hour=int(start_at[:2]), minute=int(start_at[3:]),
                                    second=0, microsecond=0)
    if target < datetime.now():
        return
    print(f"Sleeping until {target:%H:%M:%S}...")
    while (left := (target - datetime.now()).total_seconds()) > 0:
        time.sleep(min(left, 30))


def main():
    load_env_file()
    ap = argparse.ArgumentParser(description="Skyway Golf Course tee time bot")
    ap.add_argument("--login", action="store_true", help="open a browser to log in, then exit")
    ap.add_argument("--date", help="play date, YYYY-MM-DD")
    ap.add_argument("--earliest", default="06:00", help="earliest tee time, HH:MM (24h)")
    ap.add_argument("--latest", default="10:00", help="latest tee time, HH:MM (24h)")
    ap.add_argument("--players", type=int, default=2, choices=[1, 2, 3, 4])
    ap.add_argument("--start-at", help="don't start until this local time today, HH:MM")
    ap.add_argument("--poll", type=int, default=60, help="seconds between checks")
    ap.add_argument("--max-minutes", type=int, default=180, help="give up after this long")
    ap.add_argument("--dry-run", action="store_true", help="stop before the final confirm click")
    ap.add_argument("--headless", action="store_true", help="hide the browser window")
    ap.add_argument("--channel", default=None,
                    help="browser channel. Default: try real Chrome (evades "
                         "Cloudflare), fall back to bundled Chromium. Pass 'chrome'/"
                         "'msedge' to force one, or '' to force bundled Chromium.")
    ap.add_argument("--status-file", help="write live JSON status here (for the web frontend)")
    ap.add_argument("--notify-email", help="email address to send a booking confirmation to")
    args = ap.parse_args()

    if not args.login:
        if not args.date:
            ap.error("--date is required")
        datetime.strptime(args.date, "%Y-%m-%d")
    poll = max(args.poll, MIN_POLL_SECONDS)

    global STATUS_FILE
    if args.status_file:
        STATUS_FILE = Path(args.status_file)
        emit(state="starting", message="Launching browser...", date=args.date,
             earliest=args.earliest, latest=args.latest, players=args.players)

    with sync_playwright() as pw:
        ctx = launch_context(pw, args, headless=args.headless and not args.login)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if args.login:
            page.goto(CLUB_URL)
            input("Log in to Chronogolf in the browser window, then press Enter here... ")
            ctx.close()
            print(f"Session saved to {PROFILE_DIR}")
            return

        if args.start_at:
            emit(state="waiting", message=f"Waiting until {args.start_at} to start.")
            wait_until(args.start_at)

        emit(state="starting", message="Signing in to Chronogolf...")
        if not ensure_logged_in(page):
            print("  ! Could not sign in. Check CHRONO_USER/CHRONO_PASS, or run "
                  "--login by hand.")
            emit(state="error", message="Could not sign in to Chronogolf.")
            ctx.close()
            return

        give_up = datetime.now() + timedelta(minutes=args.max_minutes)
        while datetime.now() < give_up:
            print(f"[{datetime.now():%H:%M:%S}] Checking {args.date} "
                  f"{args.earliest}-{args.latest} for {args.players}...")
            emit(state="searching",
                 message=f"Checking {args.date} {args.earliest}-{args.latest} "
                         f"for {args.players}...")
            try:
                hits = check(page, args)
                if hits:
                    openings = ", ".join(label(m) for m in hits)
                    print("  Open: " + openings + "\a")
                    emit(state="found", message=f"Open slots: {openings}. Booking...")
                    if book(page, hits[0], args) or args.dry_run:
                        break
                else:
                    print("  Nothing in your window.")
                    emit(state="searching", message="Nothing in your window yet. "
                                                     "Watching for openings...")
            except Exception as e:  # keep the watcher alive through flaky page loads
                print(f"  ! {type(e).__name__}: {e}")
                emit(state="searching", message=f"Recovered from {type(e).__name__}; "
                                                 "still watching.")
            nap = nap_seconds(poll)
            print(f"  next check in {nap:.0f}s")
            time.sleep(nap)
        else:
            print("Gave up: time limit reached.")
            emit(state="gave_up", message="Gave up: time limit reached.")
        ctx.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        emit(state="stopped", message="Bot stopped.")
        raise
    except Exception as e:
        emit(state="error", message=f"{type(e).__name__}: {e}")
        raise

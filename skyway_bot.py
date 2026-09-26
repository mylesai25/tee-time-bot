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
PROFILE_DIR = Path.home() / ".skyway_bot_profile"   # real-Chrome profile dir
# One-time captured login (cookies + storage), reused so the bot doesn't have
# to sign in again and re-trigger Cloudflare. Lives in $HOME, never committed.
SESSION_FILE = Path.home() / ".skyway_session.json"
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
    # Gmail displays app passwords in 4 space-separated groups; the real
    # password has no spaces, so strip them.
    password = (os.environ.get("SKYWAY_SMTP_PASS") or "").replace(" ", "")
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


# Skyway's course UUID, used to query its tee-time API directly.
COURSE_ID = "0b833d14-8c0d-46ca-82e6-7b992de4761e"
TEETIME_API = "https://www.chronogolf.com/marketplace/v2/teetimes"


def build_url(date_str: str, players: int, holes: int = 9) -> str:
    # The public booking page for a date/party size (used to drive the UI).
    return (f"{CLUB_URL}?date={date_str}&step=teetimes"
            f"&holes={holes}&groupSize={players}")


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


def fetch_teetimes(page, date_str, players, holes=9, max_pages=4):
    """Query Skyway's tee-time API directly and return the raw slot dicts.

    Reading availability needs no login, and calling the API is far more
    reliable than trying to intercept the request the page happens to fire.
    """
    if "chronogolf.com" not in page.url:
        page.goto(CLUB_URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1000)
    slots = []
    for pg in range(1, max_pages + 1):
        url = (f"{TEETIME_API}?start_date={date_str}&free_slots={players}"
               f"&course_ids={COURSE_ID}&holes={holes}&page={pg}")
        txt = page.evaluate(
            "async (u) => (await fetch(u, {headers: {accept: 'application/json'}})).text()",
            url)
        try:
            data = json.loads(txt)
        except (ValueError, TypeError):
            break
        page_slots = data.get("teetimes") if isinstance(data, dict) else None
        if not page_slots:
            break
        slots.extend(page_slots)
        if len(page_slots) < 24:  # short page => no more results
            break
    return slots


def check(page, args):
    """Return sorted minutes-after-midnight of matching Skyway slots."""
    try:
        slots = fetch_teetimes(page, args.date, args.players, holes=args.holes)
    except Exception as e:
        print(f"  ! teetime fetch failed: {type(e).__name__}: {e}")
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
    launch_args = list(STEALTH_ARGS)
    if sys.platform.startswith("linux"):
        # Headless Linux/servers (e.g. EC2) have a tiny /dev/shm and often run as
        # a user where Chromium's sandbox can't start — these prevent crashes.
        launch_args += ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
    opts = dict(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1280, "height": 900},
        args=launch_args,
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

    Polls for the account menu, because Chronogolf renders its header
    client-side and 'My Account' can take a moment to appear. We deliberately
    do NOT early-return on seeing 'Log In': the logged-out header can flash
    briefly while the session is validated, which would false-negative.
    """
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        body = page.inner_text("body") if page.query_selector("body") else ""
        if "My Account" in body or "My account" in body or "/dashboard" in page.url:
            return True
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


# ---------- session capture & reuse ----------
# You solve the "verify you are human" check ONCE; we save the resulting
# authenticated session and re-inject it on later runs so the bot never has to
# log in again (and so never re-triggers Cloudflare). No control is bypassed —
# a human genuinely passed the check; we just don't throw the session away.

def capture_session(ctx, page):
    """Save cookies + local/session storage from a logged-in page."""
    storage = page.evaluate(
        """() => ({
            local: Object.fromEntries(Object.entries(localStorage)),
            session: Object.fromEntries(Object.entries(sessionStorage)),
        })""")
    data = {"cookies": ctx.cookies(), "storage": storage}
    SESSION_FILE.write_text(json.dumps(data))
    try:
        SESSION_FILE.chmod(0o600)  # it's a login; keep it private
    except OSError:
        pass
    print(f"  Session captured to {SESSION_FILE}")


def load_session(ctx):
    """Re-inject a previously captured session into a fresh context.

    Returns True if a session file was applied (not whether it's still valid).
    """
    if not SESSION_FILE.exists():
        return False
    try:
        data = json.loads(SESSION_FILE.read_text())
    except (OSError, ValueError):
        return False
    if data.get("cookies"):
        ctx.add_cookies(data["cookies"])
    storage = data.get("storage") or {}
    # Restore localStorage/sessionStorage on every page load (origin-scoped).
    ctx.add_init_script(
        "(() => { try { const s = " + json.dumps(storage) + ";"
        " for (const [k, v] of Object.entries(s.local || {})) localStorage.setItem(k, v);"
        " for (const [k, v] of Object.entries(s.session || {})) sessionStorage.setItem(k, v);"
        " } catch (e) {} })()")
    return True


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


# ---------- member (resident) booking ----------
# Members book only through the dashboard widget:
#   Book on Calendar -> Date -> Players -> Continue -> Choose <time> ->
#   Continue -> agree to terms -> Confirm Reservation.

DASHBOARD_MEMBERSHIPS = "https://www.chronogolf.com/dashboard/#/memberships"
WIDGET_TITLE = "Online Booking"

# The endpoint the member widget itself calls when you click Continue at the
# Players step. Before the day is released it answers 422 ("out of your
# booking range"); afterwards it returns the day's tee times. Polling it is
# therefore both the most accurate availability check for a resident and a
# precise release detector. IDs come from the widget's own request URL.
MEMBER_TEETIME_API = "https://www.chronogolf.com/marketplace/clubs/18930/teetimes"
MEMBER_COURSE_ID = 23070
RESIDENT_AFFILIATION_ID = 111289


def member_teetimes_url(date_str, players, holes=9):
    q = f"date={date_str}&course_id={MEMBER_COURSE_ID}&nb_holes={holes}"
    q += "".join(f"&affiliation_type_ids%5B%5D={RESIDENT_AFFILIATION_ID}"
                 for _ in range(players))
    return f"{MEMBER_TEETIME_API}?{q}"


def fetch_member_teetimes(page, date_str, players, holes=9):
    """Ask the member endpoint for the day. Returns (released, slots).

    released is False when the site says the day isn't bookable yet (HTTP 422),
    True when it returned a tee sheet (possibly with nothing open). Any other
    status raises.
    """
    status, body = page.evaluate(
        "async (u) => { const r = await fetch(u, {headers: {accept: 'application/json'}});"
        " return [r.status, await r.text()]; }",
        member_teetimes_url(date_str, players, holes))
    if status == 422:
        return False, []
    if status != 200:
        raise RuntimeError(f"member teetimes HTTP {status}: {body[:120]}")
    return True, list(find_slots(json.loads(body)))


def open_minutes(slots, players):
    """Sorted minutes-after-midnight of slots with room for the party."""
    return sorted({m for m in (slot_minutes(s) for s in slots if slot_fits(s, players))
                   if m is not None})


def continue_btn(page):
    return page.get_by_role("button", name="Continue").first


def widget_is_open(page):
    try:
        return page.get_by_text(WIDGET_TITLE, exact=True).first.is_visible()
    except Exception:
        return False


def select_calendar_day(page, date_str, timeout_ms=10000):
    """Click the target date in the widget's calendar (UI Bootstrap datepicker).

    Cells are zero-padded ("03", never "3"), and the grid also shows the
    neighbouring months' days, so a bare text match can hit the wrong cell or
    nothing at all. Past days are rendered as disabled buttons, and the only
    other cell with the same number is a month away (i.e. in the past), so the
    single *enabled* button with that text is always the right one. Bookings
    open 7 days out, so the target is normally already on screen; if it isn't
    (rare), step to the next month.
    """
    d = datetime.strptime(date_str, "%Y-%m-%d")
    cell = re.compile(rf"^\s*{d.day:02d}\s*$")
    page.get_by_role("button", name="Next month").first.wait_for(
        state="visible", timeout=timeout_ms)
    cells = page.locator("button:not([disabled])").filter(has_text=cell)
    if cells.count() == 0:
        page.get_by_role("button", name="Next month").first.click()
        page.wait_for_timeout(400)
    cells.first.click(timeout=timeout_ms)


def open_widget_to_players(page, date_str, players):
    """Fresh dashboard -> Book on Calendar -> date -> party size. Stops with the
    Players step open and its Continue button ready (nothing fetched yet).

    Every step waits for the next control instead of sleeping, so this takes
    ~2s instead of the ~13s of fixed pauses it used to.
    """
    # A hash-route change alone won't re-render the dashboard, so force a
    # reload to clear any widget left open by a previous attempt.
    page.goto(DASHBOARD_MEMBERSHIPS, wait_until="domcontentloaded", timeout=45000)
    page.reload(wait_until="domcontentloaded", timeout=45000)
    btn = page.get_by_role("button", name="Book on Calendar").first
    btn.wait_for(state="visible", timeout=20000)
    btn.click()
    select_calendar_day(page, date_str)
    lbl = page.locator("label.toggler-heading").filter(
        has_text=re.compile(rf"^\s*{players}\s*$")).first
    lbl.wait_for(state="visible", timeout=15000)
    lbl.click()
    # The player-type block slides open under the party-size picker; let it
    # settle so the Continue click lands after Angular has applied the types.
    page.get_by_text("player type", exact=False).first.wait_for(state="visible", timeout=15000)
    continue_btn(page).wait_for(state="visible", timeout=15000)
    page.wait_for_timeout(400)


def reopen_players_step(page):
    """If the widget has moved past the Players step, click its Edit link so
    Continue is available again (Continue re-fetches the tee sheet)."""
    if continue_btn(page).is_visible():
        return
    page.get_by_label(re.compile("Edit Players", re.I)).first.click()
    continue_btn(page).wait_for(state="visible", timeout=10000)


CHOOSE = re.compile(r"^\s*Choose\b")     # the per-row "Choose" control (not "choose your player type")


def load_teetime_list(page, timeout_ms=6000):
    """Click Continue at the Players step and wait for the tee sheet to render.

    Returns True when Choose controls are on screen, False if the widget shows
    the "out of your booking range" alert (day not released) or nothing came.
    Re-clicks Continue once if the first click didn't take.
    """
    continue_btn(page).click()
    deadline = time.time() + timeout_ms / 1000
    reclicked = False
    while time.time() < deadline:
        if page.get_by_text(CHOOSE).count() > 0:
            return True
        alert = page.get_by_role("alert").filter(has_text=re.compile("booking range", re.I))
        if alert.count() > 0 and alert.first.is_visible():
            return False
        if not reclicked and time.time() > deadline - timeout_ms / 1000 + 2.0:
            reclicked = True
            try:
                if continue_btn(page).is_visible():
                    continue_btn(page).click(timeout=1000)
            except Exception:
                pass
        page.wait_for_timeout(100)
    return False


def choose_time(page, time_label, timeout_ms=3000):
    """Click the 'Choose' on the tee-time row for an exact label like '9:00 AM'.

    The list has just been fetched, so if the row isn't there within a few
    seconds it's gone: fail fast and let the caller try the next candidate.
    """
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        ch = page.get_by_text(CHOOSE)
        for i in range(ch.count()):
            el = ch.nth(i)
            try:
                row = el.evaluate(
                    "e => { let p = e; for (let k=0;k<6;k++){ p = p.parentElement;"
                    " if (!p) break; if (/[0-9]{1,2}:[0-9]{2}/.test(p.innerText))"
                    " return p.innerText; } return ''; }")
            except Exception:
                row = ""
            if row.strip().startswith(time_label):
                el.click()
                return True
        page.wait_for_timeout(150)
    return False


def book_member(page, date_str, time_label, players, dry_run=False):
    """Book one resident tee time through the dashboard widget.

    Returns (ok, detail). If the widget is already open (pre-warmed, or left
    from a previous attempt on this run) it's reused: re-opening the Players
    step and clicking Continue re-fetches the tee sheet in well under a second,
    versus several seconds for a fresh dashboard load.
    """
    if widget_is_open(page):
        reopen_players_step(page)
    else:
        open_widget_to_players(page, date_str, players)

    if not load_teetime_list(page):
        return False, "tee sheet didn't load (day not released yet?)"
    if not choose_time(page, time_label):
        return False, f"{time_label} not available"

    # Continue -> the review page. This is the request that places the
    # 5-minute hold on the slot, so everything above is on the critical path.
    nxt = continue_btn(page)
    nxt.wait_for(state="visible", timeout=10000)
    nxt.click()
    t_hold = datetime.now()

    # Agree to the booking policy (review page).
    try:
        box = page.get_by_role("checkbox").first
        box.wait_for(state="visible", timeout=25000)
        box.check(timeout=6000)
    except Exception:
        try:
            page.get_by_text("I agree", exact=False).first.click(timeout=3000)
        except Exception:
            pass

    tag = re.sub(r"\W+", "", time_label)
    if dry_run:
        print(f"  [dry-run] reached Confirm for {time_label} (hold at {t_hold:%H:%M:%S.%f}): "
              f"{shot(page, 'dryrun_'+tag)}")
        return True, f"[dry-run] reached confirm for {time_label}"

    status = []
    page.on("response", lambda r: status.append(r.status)
            if ("marketplace/reservations" in r.url and r.request.method == "POST")
            else None)
    page.get_by_role("button", name=re.compile("Confirm Reservation", re.I)).first.click()
    ok, body = False, ""
    deadline = time.time() + 20
    while time.time() < deadline:
        body = page.inner_text("body") if page.query_selector("body") else ""
        if "successfully created" in body.lower() or 201 in status:
            ok = True
            break
        page.wait_for_timeout(300)
    conf = ""
    m = re.search(r"Booking\s+([A-Z0-9]{4}-[A-Z0-9]{4})", body)
    if m:
        conf = m.group(1)
    print(f"  {'Booked' if ok else 'FAILED'} {time_label}: {shot(page, 'booked_'+tag)}")
    return ok, (f"confirmed {conf}" if ok else "confirmation not detected")


def next_occurrence(hhmm_str):
    """Local datetime for the next HH:MM. If that time passed within the last
    10 minutes (e.g. launchd fired us a little late), treat it as now."""
    now = datetime.now()
    t = now.replace(hour=int(hhmm_str[:2]), minute=int(hhmm_str[3:]), second=0, microsecond=0)
    if t <= now:
        if (now - t).total_seconds() < 600:
            return t
        t += timedelta(days=1)
    return t


def wait_for_release(page, date_str, players, holes, release, lead_s=30, grace_s=150):
    """Sleep until just before `release`, then poll the member endpoint until
    the day appears. Returns its slots (or [] if it never did).

    Logs when the day was first seen, relative to the expected release time,
    so we learn when Chronogolf really opens the sheet. If the day is already
    open at the first poll (nothing to wait for), we still hold until the
    nominal time so a test run behaves like a real one.
    """
    while (left := (release - datetime.now()).total_seconds()) > lead_s:
        time.sleep(min(left - lead_s, 30))
    first = None
    polls = errors = 0
    while True:
        now = datetime.now()
        rel = (now - release).total_seconds()
        try:
            released, slots = fetch_member_teetimes(page, date_str, players, holes)
        except Exception as e:
            released, slots = None, []
            errors += 1
            if errors in (1, 5, 20, 50):
                print(f"  ! probe error #{errors} at T{rel:+.1f}s: {type(e).__name__}: {e}")
            time.sleep(1.0)        # back off (a 429 here would hide the release)
        polls += 1
        if first is None and released is not None:
            first = released
            print(f"[{now:%H:%M:%S.%f}] probe: day is {'already open' if released else 'not released yet'} "
                  f"(T{rel:+.1f}s)")
        if released:
            if first is False:
                print(f"[{now:%H:%M:%S.%f}] RELEASE DETECTED at T{rel:+.2f}s after {polls} polls; "
                      f"{len(open_minutes(slots, players))} slots open for {players}")
                return slots
            if rel >= 0:
                return slots
        elif rel >= grace_s:
            print(f"[{now:%H:%M:%S.%f}] ! day still not released {grace_s}s after {release:%H:%M}; going anyway")
            return []
        # Tight polling around the release moment, relaxed further out (the
        # public API rate-limits at ~1/s; this one tolerated ~2/s in testing).
        # Never sleep past the nominal time itself.
        if rel < -3:
            time.sleep(min(2.0, -rel - 3))
        elif rel < 0:
            time.sleep(min(0.15, -rel))
        else:
            time.sleep(0.15 if rel < 5 else 0.7)


def book_member_times(page, args):
    """Book each preferred time in args.times, substituting the nearest open
    foursome slot (within 2h) when a time isn't available.

    Each round after the first is placed relative to the round actually
    booked: at least 3 hours after it (MIN_GAP), and ideally the same gap that
    was originally requested between the two times (e.g. 7:00 -> 10:30 = 3.5h).
    So if the first round slips to 7:20, the second aims for ~10:50, not 10:30.
    """
    NEAREST_WINDOW = 120   # substitute a slot within 2h of the wanted time
    MIN_GAP = 180          # each round must be >= 3h after the previous one

    given = [hhmm(t.strip()) for t in args.times.split(",") if t.strip()]

    if args.release_at:
        # Pre-warm: get the widget to the Players step *before* the sheet opens,
        # so at release the only work left is Continue -> Choose -> Continue.
        release = next_occurrence(args.release_at)
        emit(state="waiting", message=f"Pre-warming; sheet opens at {release:%H:%M}.")
        try:
            open_widget_to_players(page, args.date, args.players)
            print(f"[{datetime.now():%H:%M:%S}] pre-warmed widget to Players step for "
                  f"{args.date} ({(release - datetime.now()).total_seconds():.0f}s to release)")
        except Exception as e:
            print(f"  ! pre-warm failed ({type(e).__name__}: {e}); will open the widget at release")
        slots = wait_for_release(page, args.date, args.players, args.holes, release)
        print(f"[{datetime.now():%H:%M:%S.%f}] go")
    else:
        # Times with room for the whole party, as the member widget sees them.
        try:
            released, slots = fetch_member_teetimes(page, args.date, args.players, args.holes)
            if not released:
                print(f"  ! {args.date} is not open for booking yet")
        except Exception as e:
            print(f"  ! could not read availability: {type(e).__name__}: {e}")
            slots = []
    available = open_minutes(slots, args.players)

    booked_mins = []
    results = []
    for i, orig in enumerate(given):
        if booked_mins:
            # Keep the originally-requested gap from the round we actually booked,
            # but never less than the 3-hour minimum between rounds.
            target = booked_mins[-1] + max(orig - given[i - 1], MIN_GAP)
        else:
            target = orig
        want = label(target)
        # Nearest-first candidates: within 2h of the wanted time, not already
        # booked, and at least 3h after every round already booked this run.
        cands = sorted(
            (m for m in available
             if abs(m - target) <= NEAREST_WINDOW and m not in booked_mins
             and all(m - b >= MIN_GAP for b in booked_mins)),
            key=lambda m: (abs(m - target), m))
        gapnote = f" (needs 3h+ after {label(booked_mins[-1])})" if booked_mins else ""
        outcome = (want, False, f"no open slot within 2h of {want}{gapnote}")
        for m in cands[:6]:
            lbl = label(m)
            how = "exact" if m == target else f"nearest to {want}"
            print(f"[{datetime.now():%H:%M:%S}] Booking {lbl} ({how}) on "
                  f"{args.date} for {args.players}...")
            emit(state="booking", message=f"Booking {lbl} on {args.date}...")
            try:
                ok, detail = book_member(page, args.date, lbl, args.players,
                                         dry_run=args.dry_run)
            except Exception as e:
                ok, detail = False, f"{type(e).__name__}: {e}"
            print(f"  -> {'OK' if ok else 'FAIL'}: {detail}")
            if ok:
                outcome = (lbl, True, f"{how}; {detail}")
                booked_mins.append(m)
                available.remove(m)
                break
            outcome = (lbl, False, f"{how}; {detail}")
        if not outcome[1]:
            print(f"[{datetime.now():%H:%M:%S}] {want}: could not book — {outcome[2]}")
        results.append(outcome)
    booked = [r for r in results if r[1]]
    emit(state="booked" if booked else "error",
         message="; ".join(f"{l}: {'ok' if ok else 'fail'}" for l, ok, _ in results),
         booked_date=args.date)

    # Email one summary per run (successes and failures), so you always know the
    # outcome. Recipient comes from --notify-email or SKYWAY_NOTIFY_EMAIL (.env).
    recipient = args.notify_email or os.environ.get("SKYWAY_NOTIFY_EMAIL")
    if recipient and not args.dry_run:
        lines = [f"{'BOOKED ' if ok else 'FAILED '} {lbl}: {detail}"
                 for lbl, ok, detail in results]
        subject = (f"Skyway {args.date}: {len(booked)}/{len(results)} booked "
                   f"({args.players} players)")
        body = (f"Tee time booking run for {args.date}, {args.players} players:\n\n"
                + "\n".join(lines) + "\n")
        _, sdetail = send_confirmation_email(recipient, subject, body)
        print(f"  summary email: {sdetail}")
    return results


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
    ap.add_argument("--login", action="store_true",
                    help="open a browser to log in once; the session is captured "
                         "and reused so later runs don't sign in again")
    ap.add_argument("--date", help="play date, YYYY-MM-DD")
    ap.add_argument("--earliest", default="06:00", help="earliest tee time, HH:MM (24h)")
    ap.add_argument("--latest", default="10:00", help="latest tee time, HH:MM (24h)")
    ap.add_argument("--players", type=int, default=2, choices=[1, 2, 3, 4])
    ap.add_argument("--holes", type=int, default=9, choices=[9, 18], help="9 or 18 holes")
    ap.add_argument("--start-at", help="don't start until this local time today, HH:MM")
    ap.add_argument("--release-at", help="resident booking: local HH:MM at which the "
                    "target day opens (e.g. 00:00). Signs in and pre-warms the widget "
                    "beforehand, then books the instant the sheet appears.")
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
    ap.add_argument("--times", help="resident booking: comma-separated preferred "
                    "tee times, HH:MM (e.g. 07:00,10:30). Books the nearest open "
                    "slot within 2h if the exact time isn't available.")
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
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            print("\nA Chrome window is open. Log in to Chronogolf and solve the")
            print("'verify you are human' check if it appears.")
            input("Once you can see your account / dashboard, press Enter here... ")
            page.goto(CLUB_URL, wait_until="domcontentloaded")
            if is_logged_in(page, timeout_ms=15000):
                capture_session(ctx, page)
                print("Login captured. Future runs will reuse it — no re-login.")
            else:
                print("! That didn't look logged in; nothing captured. Try again.")
            ctx.close()
            return

        # Reuse a previously captured login so we don't sign in (and re-trigger
        # Cloudflare) again.
        reused = load_session(ctx)

        if args.start_at:
            emit(state="waiting", message=f"Waiting until {args.start_at} to start.")
            wait_until(args.start_at)

        emit(state="starting", message="Signing in to Chronogolf...")
        if not ensure_logged_in(page):
            hint = ("Session expired — run `--login` once to refresh it."
                    if reused else
                    "Run `--login` once to capture your session.")
            print(f"  ! Not signed in. {hint}")
            emit(state="error", message=f"Not signed in. {hint}")
            ctx.close()
            return

        # Resident booking of specific tee times (dashboard widget flow).
        if args.times:
            book_member_times(page, args)
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

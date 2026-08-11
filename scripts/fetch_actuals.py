#!/usr/bin/env python3
"""
scripts/fetch_actuals.py                                                v1.0.0

Fills in the `actual` field on calendar.json.

WHY THIS EXISTS
    ForexFactory's weekly JSON feed (ff_calendar_thisweek.json), which
    fetch_calendar.py mirrors, carries forecast and previous but has no actual
    column. It never has. Confirmed against a live bkt-calendar /admin/session
    dump on 2026-08-11: the AUD Cash Rate released at 12:30 AM ET and still
    read "actual": "" hours later, as did every other released row.

    The released value only exists in the calendar HTML page. This script
    scrapes that page and merges the values onto the file fetch_calendar.py
    already produced.

DESIGN: ADDITIVE POST-PROCESSING
    This runs AFTER fetch_calendar.py and treats calendar.json as its input.
    It knows nothing about how that file was built: not the id scheme, not the
    rolling 14 day merge, not the normalization. It only ever fills EMPTY
    actual fields on events that have already passed.

    That separation is deliberate. If ForexFactory changes its HTML, or blocks
    the runner, or the parse returns nothing, this script exits 0 with a
    warning and calendar.json keeps its schedule data intact. A broken scrape
    must never take down the calendar itself, which is the whole reason the
    mirror exists.

MATCHING
    On the tuple (currency, normalized title, Eastern calendar date), NOT on
    id. The ids in calendar.json are hashes minted by fetch_calendar.py and
    nothing in the HTML corresponds to them. The tuple is stable across any
    change to id generation on either side.

BETTER/WORSE
    ForexFactory colours the actual green when it beat and red when it missed,
    and that classification already encodes direction per indicator: higher is
    better for GDP, worse for jobless claims. We capture FF's own class rather
    than comparing numbers ourselves, because inferring direction indicator by
    indicator is a maintenance trap that rots silently.

USAGE
    python scripts/fetch_actuals.py                 # merge into calendar.json
    python scripts/fetch_actuals.py --probe         # fetch only, print what
                                                    # was parsed, write nothing
    python scripts/fetch_actuals.py --days 3        # how far back to backfill

EXIT CODES
    0  success, or a soft failure that left calendar.json untouched
    1  calendar.json missing or unparseable (a real problem, fail the job)
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("FATAL: beautifulsoup4 not installed. Add it to the workflow:")
    print("       pip install beautifulsoup4")
    sys.exit(1)

CALENDAR_PATH = os.environ.get("CALENDAR_PATH", "calendar.json")
ET = ZoneInfo("America/New_York")

BASE = "https://www.forexfactory.com/calendar?day="

# A GitHub runner is not a browser. ForexFactory sits behind Cloudflare and
# will challenge an obvious bot UA. This is the single most likely point of
# failure in the whole script; run with --probe first to confirm the runner
# can fetch at all before wiring it into the schedule.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

TIMEOUT = 30
RETRIES = 3
BACKOFF = 20  # seconds, doubled each retry

# Currencies bkt-calendar covers. Anything else in the HTML is ignored.
COVERED = {"USD", "EUR", "GBP", "JPY", "CAD", "CHF", "AUD", "NZD"}


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def norm_title(s):
    """
    Loose title key. Both sides originate at ForexFactory so they agree closely,
    but the HTML carries non-breaking spaces and occasional double spacing that
    the JSON feed does not. Lowercase, strip anything that is not alphanumeric.
    """
    s = (s or "").replace("\u00a0", " ")
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def et_date_key(event):
    """
    Eastern calendar date for an event, as ForexFactory's page organises days.

    Prefers local_date, which fetch_calendar.py carries and which is already
    the ET date from the original offset-bearing string. Falls back to
    converting the UTC instant, which is correct but redundant when local_date
    is present.
    """
    ld = (event.get("local_date") or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", ld):
        return ld
    inst = (event.get("instant") or "").strip()
    if not inst:
        return None
    try:
        dt = datetime.fromisoformat(inst.replace("Z", "+00:00"))
        return dt.astimezone(ET).date().isoformat()
    except ValueError:
        return None


def ff_day_param(date_iso):
    """ForexFactory wants ?day=aug11.2026 style, lowercase month abbreviation."""
    d = datetime.fromisoformat(date_iso).date()
    return f"{d.strftime('%b').lower()}{d.day}.{d.year}"


def fetch(url):
    """GET with retries. Returns HTML text, or None on a soft failure."""
    delay = BACKOFF
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            if "calendar__row" not in body and "calendar_row" not in body:
                # Served something, but not the calendar. Almost always a
                # Cloudflare interstitial or a consent wall.
                print(f"  WARN attempt {attempt}: response has no calendar rows "
                      f"({len(body)} bytes), likely a bot challenge")
            else:
                return body
        except urllib.error.HTTPError as e:
            print(f"  WARN attempt {attempt}: HTTP {e.code}")
        except Exception as e:
            print(f"  WARN attempt {attempt}: {e}")
        if attempt < RETRIES:
            time.sleep(delay)
            delay *= 2
    return None


# ----------------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------------

def parse_day(html):
    """
    Extract released values from one calendar day page.

    Returns {(ccy, norm_title): {"actual": str, "actual_class": str}}.

    ForexFactory's markup, as of writing:
        tr.calendar__row
          td.calendar__currency   -> "AUD"
          td.calendar__event      -> "Cash Rate"
          td.calendar__actual     -> "3.60%", with a span carrying
                                     class "better" or "worse" when FF has
                                     coloured it green or red
    Class names have changed before. If this returns nothing while --probe
    shows the page fetching fine, the selectors below are what to fix.
    """
    soup = BeautifulSoup(html, "html.parser")
    out = {}

    rows = soup.select("tr.calendar__row") or soup.select("tr.calendar_row")
    for row in rows:
        ccy_el = row.select_one(".calendar__currency") or row.select_one(".calendar_currency")
        ev_el = row.select_one(".calendar__event") or row.select_one(".calendar_event")
        act_el = row.select_one(".calendar__actual") or row.select_one(".calendar_actual")
        if not (ccy_el and ev_el and act_el):
            continue

        ccy = ccy_el.get_text(strip=True).upper()
        if ccy not in COVERED:
            continue

        title = ev_el.get_text(" ", strip=True)
        actual = act_el.get_text(strip=True)
        if not actual or actual in {"-", "\u2014"}:
            continue

        # FF's own better/worse classification. Preferred over comparing
        # numbers, which would need per-indicator direction knowledge.
        cls = ""
        span = act_el.find("span")
        classes = (span.get("class") if span else None) or act_el.get("class") or []
        for c in classes:
            if c in ("better", "worse"):
                cls = c
                break

        out[(ccy, norm_title(title))] = {"actual": actual, "actual_class": cls}

    return out


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true",
                    help="fetch and parse only, write nothing")
    ap.add_argument("--days", type=int, default=2,
                    help="how many days back to backfill (default 2)")
    args = ap.parse_args()

    try:
        with open(CALENDAR_PATH, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        print(f"FATAL: cannot read {CALENDAR_PATH}: {e}")
        return 1

    events = doc.get("events")
    if not isinstance(events, list) or not events:
        print(f"FATAL: {CALENDAR_PATH} has no events array")
        return 1

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=args.days)

    # Only chase days that actually have a gap: an event that has already
    # passed and still carries no actual. No point fetching a day that is
    # either fully backfilled or entirely in the future.
    wanted = set()
    for ev in events:
        inst = (ev.get("instant") or "").strip()
        if not inst:
            continue
        try:
            dt = datetime.fromisoformat(inst.replace("Z", "+00:00"))
        except ValueError:
            continue
        if not (cutoff <= dt <= now):
            continue
        if ev.get("all_day"):
            continue
        if str(ev.get("actual") or "").strip():
            continue
        key = et_date_key(ev)
        if key:
            wanted.add(key)

    if not wanted:
        print("Nothing to backfill: every passed event already has an actual.")
        return 0

    print(f"Days needing actuals: {', '.join(sorted(wanted))}")

    scraped = {}
    for day in sorted(wanted):
        url = BASE + ff_day_param(day)
        print(f"Fetching {url}")
        html = fetch(url)
        if not html:
            print(f"  FAILED, skipping {day}")
            continue
        found = parse_day(html)
        print(f"  parsed {len(found)} released values")
        for k, v in found.items():
            scraped[(day, k[0], k[1])] = v

    if not scraped:
        # Soft failure. calendar.json keeps its schedule data and the Worker
        # carries on exactly as before, just without actuals.
        print("WARNING: no actuals parsed from any day. calendar.json untouched.")
        print("         Run with --probe to see whether the fetch or the parse failed.")
        return 0

    if args.probe:
        print(f"\nPROBE, {len(scraped)} values, writing nothing:")
        for (day, ccy, t), v in sorted(scraped.items()):
            flag = f" [{v['actual_class']}]" if v["actual_class"] else ""
            print(f"  {day} {ccy} {t[:40]:40} {v['actual']}{flag}")
        return 0

    filled = 0
    for ev in events:
        # Never overwrite a value that is already there. Only fill gaps.
        if str(ev.get("actual") or "").strip():
            continue
        day = et_date_key(ev)
        if not day:
            continue
        hit = scraped.get((day, (ev.get("ccy") or "").upper(),
                           norm_title(ev.get("title"))))
        if not hit:
            continue
        ev["actual"] = hit["actual"]
        if hit["actual_class"]:
            ev["actual_class"] = hit["actual_class"]
        filled += 1

    if not filled:
        print("Parsed values but matched none. Check title normalization.")
        return 0

    doc["actuals_updated_at"] = now.isoformat().replace("+00:00", "Z")
    tmp = CALENDAR_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CALENDAR_PATH)

    print(f"Filled {filled} actual values into {CALENDAR_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

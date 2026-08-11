#!/usr/bin/env python3
"""
scripts/fetch_actuals.py                                                v1.1.0

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
    On (currency, normalized title) plus a date within one day, NOT on id. The
    ids in calendar.json are hashes minted by fetch_calendar.py and nothing in
    the HTML corresponds to them.

    THE ONE DAY TOLERANCE IS NOT SLOPPINESS, it is required. ForexFactory
    serves a logged-out client in fixed EST (UTC-5) with no daylight saving,
    while calendar.json's local_date is true America/New_York, which is EDT
    (UTC-4) in summer. Any event in the first hour after ET midnight therefore
    sits on the PREVIOUS day's FF page.

    Caught on the RBA Cash Rate of 2026-08-11T04:30:00Z. That is 00:30 EDT on
    Aug 11, so local_date reads 2026-08-11, but FF grouped it under aug10 and
    an exact-day match skipped the most important row on the board. Verified
    against a live probe run, not theorised.

    Exact-day matches are still preferred and only fall back to the adjacent
    day when there is no exact hit, so a recurring release cannot be pulled
    off the wrong day while its own day is present.

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

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_ff_date(text, ref_iso):
    """
    "Mon Aug 10" -> "2026-08-10". FF omits the year, so it is taken from the
    requested day, with a rollover guard for the Dec/Jan boundary.
    """
    m = re.search(r"([A-Za-z]{3})\s*(\d{1,2})", text or "")
    if not m:
        return None
    mon = MONTHS.get(m.group(1).lower())
    if not mon:
        return None
    day = int(m.group(2))
    ref = datetime.fromisoformat(ref_iso).date()
    for year in (ref.year, ref.year - 1, ref.year + 1):
        try:
            cand = datetime(year, mon, day).date()
        except ValueError:
            continue
        if abs((cand - ref).days) <= 200:
            return cand.isoformat()
    return None


def parse_day(html, requested_day):
    """
    Extract released values from one calendar day page.

    Returns {(ccy, norm_title): {day_iso: {"actual": str, "actual_class": str}}}.

    Rows are attributed to the day in FF's own date header rather than to the
    requested page, because a day page can carry spillover rows from the
    neighbouring day. The header cell is populated only on the first row of
    each day and blank thereafter, so the last seen value carries forward.

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
    current_day = requested_day
    for row in rows:
        date_el = row.select_one(".calendar__date") or row.select_one(".calendar_date")
        if date_el:
            hdr = parse_ff_date(date_el.get_text(" ", strip=True), requested_day)
            if hdr:
                current_day = hdr
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

        out.setdefault((ccy, norm_title(title)), {})[current_day] = {
            "actual": actual, "actual_class": cls
        }

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
        found = parse_day(html, day)
        n = sum(len(v) for v in found.values())
        print(f"  parsed {n} released values")
        for key, by_day in found.items():
            scraped.setdefault(key, {}).update(by_day)

    if not any(scraped.values()):
        # Soft failure. calendar.json keeps its schedule data and the Worker
        # carries on exactly as before, just without actuals.
        print("WARNING: no actuals parsed from any day. calendar.json untouched.")
        print("         Run with --probe to see whether the fetch or the parse failed.")
        return 0

    if args.probe:
        total = sum(len(v) for v in scraped.values())
        print(f"\nPROBE, {total} values, writing nothing:")
        flat = sorted((d, c, t, v) for (c, t), byd in scraped.items()
                      for d, v in byd.items())
        for day, ccy, t, v in flat:
            flag = f" [{v['actual_class']}]" if v["actual_class"] else ""
            print(f"  {day} {ccy} {t[:40]:40} {v['actual']}{flag}")
        return 0

    filled = 0
    off_by_one = []
    for ev in events:
        # Never overwrite a value that is already there. Only fill gaps.
        if str(ev.get("actual") or "").strip():
            continue
        day = et_date_key(ev)
        if not day:
            continue
        by_day = scraped.get(((ev.get("ccy") or "").upper(),
                              norm_title(ev.get("title"))))
        if not by_day:
            continue
        # Exact day first. Only fall back to the adjacent day when the event's
        # own day carries no value, so a recurring release is never pulled off
        # the wrong date while its own date is present. The fallback exists
        # because FF serves a logged-out client in fixed EST while local_date
        # is EDT in summer, which shifts post-midnight events back a day.
        hit = by_day.get(day)
        if not hit:
            d0 = datetime.fromisoformat(day).date()
            for delta in (-1, 1):
                alt = (d0 + timedelta(days=delta)).isoformat()
                if alt in by_day:
                    hit = by_day[alt]
                    off_by_one.append(f"{ev.get('ccy')} {ev.get('title')}")
                    break
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
    if off_by_one:
        print(f"  {len(off_by_one)} matched on the adjacent day (FF EST vs ET EDT): "
              + ", ".join(off_by_one[:5]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

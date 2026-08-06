#!/usr/bin/env python3
"""
BK Traders economic calendar mirror
-----------------------------------
Runs on a GitHub Actions runner, NOT on Cloudflare. GitHub runners have their
own egress IPs, so ForexFactory's per-IP rate limit (roughly 2 weekly-file pulls
every 5 minutes, shared across every Cloudflare Worker tenant) stops applying.

Pulls the thisweek and nextweek feeds, normalizes them into one flat schema and
merges them into calendar.json. The bkt-calendar Worker then reads that file
over raw.githubusercontent.com, which is a CDN and will never rate limit us.

Coverage: G-7 plus Switzerland, Australia and New Zealand. Every impact level
including holidays, so the Worker can filter rather than the mirror.

Safety rules baked in:
  - Never stomp good data. New events are MERGED over the existing file by
    stable id. A failed pull leaves the previous rows intact.
  - Never write an empty or obviously broken file. Below MIN_EVENTS the script
    exits without touching calendar.json, so no bad commit lands.
  - Rows older than PRUNE_DAYS are dropped so the file cannot grow forever.
"""

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ---------------------------------------------------------------- config

FEEDS = [
    ("thisweek", "https://nfs.faireconomy.media/ff_calendar_thisweek.json"),
    ("nextweek", "https://nfs.faireconomy.media/ff_calendar_nextweek.json"),
]

# G-7 (US, UK, Canada, Japan, and EUR for Germany/France/Italy) plus CHF, AUD, NZD.
KEEP_CCY = {"USD", "EUR", "GBP", "JPY", "CAD", "CHF", "AUD", "NZD"}

OUT_PATH = "calendar.json"

# Gap between the two feed pulls. FF allows roughly 2 pulls per 5 minutes per IP.
# Three minutes keeps us comfortably inside that even on a retry.
FEED_GAP_SECONDS = 180

RETRIES = 3
RETRY_BACKOFF_SECONDS = 45
TIMEOUT_SECONDS = 30

# Sanity floor. A normal two-week window carries several hundred rows. Anything
# under this means the pull is broken and must not overwrite a good file.
MIN_EVENTS = 40

# Drop rows older than this so the mirror stays a rolling window.
PRUNE_DAYS = 14

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

IMPACT_MAP = {
    "high": "high",
    "medium": "medium",
    "low": "low",
    "holiday": "holiday",
    "non-economic": "holiday",
}


# ---------------------------------------------------------------- fetching


def fetch_feed(name, url):
    """Fetch one FF weekly file with retries. Returns a list, or None on failure."""
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = Request(url, headers={"Accept": "application/json", "User-Agent": UA})
            with urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            stripped = body.lstrip()
            if stripped.startswith("<"):
                raise ValueError("feed returned HTML (rate limited or blocked)")
            data = json.loads(body)
            if not isinstance(data, list):
                raise ValueError("feed shape unexpected, expected a JSON array")
            print(f"[{name}] ok, {len(data)} raw rows (attempt {attempt})")
            return data
        except (HTTPError, URLError, ValueError, json.JSONDecodeError, OSError) as e:
            last_err = e
            print(f"[{name}] attempt {attempt} failed: {e}", file=sys.stderr)
            if attempt < RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    print(f"[{name}] GIVING UP: {last_err}", file=sys.stderr)
    return None


# ---------------------------------------------------------------- normalizing


def parse_instant(raw):
    """FF ships ISO-8601 with an offset, e.g. 2026-08-05T08:30:00-04:00."""
    if not raw:
        return None
    txt = str(raw).strip()
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Return both the UTC instant and the ORIGINAL local calendar date. All-day
    # rows (bank holidays, tentative releases) sit at local midnight, so
    # converting them to UTC can shove them onto the previous or next day. The
    # Worker renders those by local_date instead of by instant.
    return dt.astimezone(timezone.utc), dt.strftime("%Y-%m-%d")


def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def stable_id(ccy, title, instant_iso):
    raw = f"{ccy}|{title}|{instant_iso}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def normalize(raw_events, feed_name):
    """Map FF rows onto our flat schema, dropping currencies we do not cover."""
    out = []
    skipped_ccy = 0
    skipped_date = 0
    for ev in raw_events or []:
        if not isinstance(ev, dict):
            continue

        ccy = clean(ev.get("country") or ev.get("currency")).upper()
        if ccy not in KEEP_CCY:
            skipped_ccy += 1
            continue

        title = clean(ev.get("title") or ev.get("event"))
        if not title:
            continue

        parsed = parse_instant(ev.get("date") or ev.get("datetime"))
        if parsed is None:
            skipped_date += 1
            continue
        instant, local_date = parsed
        instant_iso = instant.strftime("%Y-%m-%dT%H:%M:%SZ")

        impact_raw = clean(ev.get("impact")).lower()
        impact = IMPACT_MAP.get(impact_raw, "low")

        # FF marks all-day rows (bank holidays, tentative releases) at midnight
        # local. Flag them so the Worker can render them without a clock time.
        all_day = bool(impact == "holiday" or clean(ev.get("allDay")).lower() == "true")

        out.append(
            {
                "id": stable_id(ccy, title, instant_iso),
                "ccy": ccy,
                "title": title,
                "impact": impact,
                "instant": instant_iso,
                "local_date": local_date,
                "all_day": all_day,
                "forecast": clean(ev.get("forecast")),
                "previous": clean(ev.get("previous")),
                "actual": clean(ev.get("actual")),
                "source": "forexfactory",
                "feed": feed_name,
            }
        )

    print(
        f"[{feed_name}] normalized {len(out)} rows "
        f"(skipped {skipped_ccy} off-currency, {skipped_date} unparseable dates)"
    )
    return out


# ---------------------------------------------------------------- merge and write


def load_existing(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        events = doc.get("events")
        return events if isinstance(events, list) else []
    except (OSError, ValueError) as e:
        print(f"[merge] could not read existing {path}: {e}", file=sys.stderr)
        return []


def merge(existing, fresh):
    """Fresh rows win on id collision so revised forecasts and actuals land."""
    by_id = {}
    for ev in existing:
        if isinstance(ev, dict) and ev.get("id"):
            by_id[ev["id"]] = ev
    for ev in fresh:
        by_id[ev["id"]] = ev
    return list(by_id.values())


def prune(events, now):
    cutoff = (now - timedelta(days=PRUNE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [ev for ev in events if str(ev.get("instant", "")) >= cutoff]


def main():
    now = datetime.now(timezone.utc)

    fresh = []
    feeds_ok = []
    feeds_failed = []

    for idx, (name, url) in enumerate(FEEDS):
        if idx > 0:
            print(f"[wait] sleeping {FEED_GAP_SECONDS}s to stay under the FF rate limit")
            time.sleep(FEED_GAP_SECONDS)
        raw = fetch_feed(name, url)
        if raw is None:
            feeds_failed.append(name)
            continue
        feeds_ok.append(name)
        fresh.extend(normalize(raw, name))

    if not feeds_ok:
        print("[abort] every feed failed, leaving calendar.json untouched", file=sys.stderr)
        return 0

    existing = load_existing(OUT_PATH)
    merged = prune(merge(existing, fresh), now)
    merged.sort(key=lambda ev: (ev.get("instant", ""), ev.get("ccy", ""), ev.get("title", "")))

    if len(merged) < MIN_EVENTS:
        print(
            f"[abort] merged file has only {len(merged)} rows, below the {MIN_EVENTS} "
            f"floor. Leaving calendar.json untouched.",
            file=sys.stderr,
        )
        return 0

    by_impact = {}
    for ev in merged:
        by_impact[ev["impact"]] = by_impact.get(ev["impact"], 0) + 1

    doc = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "forexfactory weekly feeds, mirrored via GitHub Actions",
        "currencies": sorted(KEEP_CCY),
        "window_days": PRUNE_DAYS,
        "feeds_ok": feeds_ok,
        "feeds_failed": feeds_failed,
        "count": len(merged),
        "count_by_impact": by_impact,
        "first_instant": merged[0]["instant"],
        "last_instant": merged[-1]["instant"],
        "events": merged,
    }

    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False, sort_keys=False)
        fh.write("\n")

    print(
        f"[done] wrote {OUT_PATH}: {len(merged)} events, "
        f"{doc['first_instant']} to {doc['last_instant']}, breakdown {by_impact}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

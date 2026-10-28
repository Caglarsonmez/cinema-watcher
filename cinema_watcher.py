#!/usr/bin/env python3
"""Cinema City CZ ticket watcher.

Polls the Cinema City JSON API and sends a push notification (via ntfy.sh)
whenever a new, bookable screening matching your filters appears.

Usage:
    python3 cinema_watcher.py          # normal run (put this in cron/launchd)
    python3 cinema_watcher.py --test   # send a test notification and exit
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

# ----------------------------- config ---------------------------------
FILM_ID = "8105s2r"                 # Dune: Part Three
GROUP = "prague"
FROM_DATE = "2026-12-21"            # only care about this date and later
REQUIRED_ATTRS = {"70-mm"}          # set() = any format
CINEMA_IDS = set()                  # e.g. {"1052"} = Flora only; empty = all of Prague
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "CHANGE-ME")  # env var wins (used by GitHub Actions)
FAIL_ALERT_AFTER = 6                # alert if the script fails this many runs in a row
# -----------------------------------------------------------------------

BASE = "https://www.cinemacity.cz/cz/data-api-service/v1/quickbook/10101"
LANG = "en_GB"
FILM_PAGE = f"https://www.cinemacity.cz/films/duna-cast-treti/{FILM_ID}"
STATE_FILE = Path(__file__).with_name("state.json")
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15"


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def notify(title, message, click=None, priority="high"):
    headers = {"Title": title, "Priority": priority, "Tags": "movie_camera"}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    urllib.request.urlopen(req, timeout=20).read()


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"notified": [], "failures": 0}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def find_new_events(notified):
    """Return bookable screenings matching the filters that we haven't announced yet."""
    until = (date.today() + timedelta(days=365)).isoformat()
    dates_url = f"{BASE}/dates/in-group/{GROUP}/with-film/{FILM_ID}/until/{until}?attr=&lang={LANG}"
    dates = [d for d in get_json(dates_url)["body"]["dates"] if d >= FROM_DATE]

    found = []
    for d in dates:
        events_url = f"{BASE}/cinema-events/in-group/{GROUP}/with-film/{FILM_ID}/at-date/{d}?attr=&lang={LANG}"
        body = get_json(events_url)["body"]
        cinema_names = {c["id"]: c["displayName"] for c in body.get("cinemas", [])}
        for ev in body.get("events", []):
            if ev["filmId"] != FILM_ID or ev.get("soldOut"):
                continue
            if CINEMA_IDS and ev["cinemaId"] not in CINEMA_IDS:
                continue
            if not REQUIRED_ATTRS.issubset(ev.get("attributeIds", [])):
                continue
            if ev["id"] in notified:
                continue
            ev["_cinema"] = cinema_names.get(ev["cinemaId"], ev["cinemaId"])
            found.append(ev)
        time.sleep(1)  # be polite
    found.sort(key=lambda e: e["eventDateTime"])
    return found


def run():
    state = load_state()
    new = find_new_events(set(state["notified"]))

    if new:
        lines = []
        for ev in new:
            when = datetime.fromisoformat(ev["eventDateTime"]).strftime("%a %d %b %H:%M")
            seats = int(ev.get("availabilityRatio", 0) * 100)
            lines.append(f"{when} | {ev['_cinema']} | {ev['auditorium']} | ~{seats}% free")
        click = new[0]["bookingRouterLaunchLink"] if len(new) == 1 else FILM_PAGE
        notify(f"{len(new)} new Dune screening(s) on sale", "\n".join(lines), click=click)
        state["notified"] += [ev["id"] for ev in new]  # only after a successful send

    state["failures"] = 0
    save_state(state)
    print(f"{datetime.now():%Y-%m-%d %H:%M} ok, {len(new)} new")


def main():
    if "--test" in sys.argv:
        notify("Cinema watcher test", "If you see this, notifications work.", priority="default")
        print("test notification sent")
        return

    try:
        run()
    except Exception as exc:  # network error, API change, etc.
        state = load_state()
        state["failures"] = state.get("failures", 0) + 1
        save_state(state)
        print(f"{datetime.now():%Y-%m-%d %H:%M} FAILED ({state['failures']}x): {exc!r}", file=sys.stderr)
        if state["failures"] == FAIL_ALERT_AFTER:
            try:
                notify("Cinema watcher is failing", f"{state['failures']} failed runs in a row: {exc!r}")
            except Exception:
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()

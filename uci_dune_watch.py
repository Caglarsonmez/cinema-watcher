#!/usr/bin/env python3
"""
Watch UCI Kinowelt Berlin East Side Gallery for Dune: Part Three screenings
on or after a cutoff date, and push a notification via ntfy.

The page is server-rendered: every screening is an <a class="badge-performance">
with data-date (YYYYMMDD), data-time, data-version, data-tracking-film-id, etc.
UCI only publishes screenings a limited number of days ahead, so the bot simply
waits until the booking window reaches the cutoff date.

Usage:
    NTFY_TOPIC=my-topic python uci_dune_watch.py        # normal run
    python uci_dune_watch.py --debug                     # fetch + print what was found, no notify
    python uci_dune_watch.py --dry-run                   # print what would be notified, no state change
    python uci_dune_watch.py --file debug.html --debug   # parse a saved page instead of fetching

Env vars:
    NTFY_TOPIC   (required for real runs)
    NTFY_SERVER  (default https://ntfy.sh)
    CUTOFF       (default 2026-12-24, YYYY-MM-DD)
    STATE_FILE   (default state.json)

Exit codes: 0 ok, 1 fetch error, 2 config error, 3 page parsed but zero Dune
screenings found at all (layout changed or film removed - worth a look).
"""
import argparse
import datetime as dt
import json
import os
import sys
from dataclasses import dataclass
from html.parser import HTMLParser

import requests

URL = "https://www.uci-kinowelt.de/film/dune-part-three/412575/berlin-east-side-gallery"
FILM_ID = "412575"
CUTOFF = dt.date.fromisoformat(os.environ.get("CUTOFF", "2026-12-24"))
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}


@dataclass(frozen=True)
class Screening:
    start: dt.datetime
    version: str      # e.g. "imax|ov|2d"
    auditorium: str   # e.g. "Kino 01 IMAX"
    link: str

    @property
    def key(self) -> str:
        # stable identity: booking perf_id if present, else start+auditorium
        return self.link.split("perf_id=")[-1].split("&")[0] if "perf_id=" in self.link \
            else f"{self.start.isoformat()}|{self.auditorium}"

    def describe(self) -> str:
        return f"{self.start:%a %d.%m. %H:%M}  {self.version.replace('|', ' ').upper()}  ({self.auditorium})"


class PerformanceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.screenings: list[Screening] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        a = dict(attrs)
        if "badge-performance" not in (a.get("class") or ""):
            return
        if a.get("data-tracking-film-id") != FILM_ID:
            return
        try:
            start = dt.datetime.strptime(f"{a['data-date']} {a['data-time']}", "%Y%m%d %H:%M")
        except (KeyError, ValueError):
            return
        self.screenings.append(Screening(
            start=start,
            version=a.get("data-version", ""),
            auditorium=a.get("data-tracking-auditorium", ""),
            link=a.get("href", ""),
        ))


def parse(html: str) -> list[Screening]:
    p = PerformanceParser()
    p.feed(html)
    return sorted(set(p.screenings), key=lambda s: s.start)


def fetch() -> str:
    r = requests.get(URL, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} from {URL} (bot protection?)")
    return r.text


def load_state() -> set[str]:
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f).get("notified", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_state(notified: set[str]) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump({"notified": sorted(notified)}, f, indent=2)


def notify(new: list[Screening]) -> None:
    lines = [s.describe() for s in new[:12]]
    if len(new) > 12:
        lines.append(f"... and {len(new) - 12} more")
    r = requests.post(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data="\n".join(lines).encode("utf-8"),
        headers={
            "Title": f"Dune Part Three - UCI East Side Gallery ({len(new)} new)",
            "Priority": "high",
            "Tags": "movie_camera",
            "Click": new[0].link or URL,
        },
        timeout=15,
    )
    r.raise_for_status()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true", help="print findings, don't notify or touch state")
    ap.add_argument("--dry-run", action="store_true", help="print what would be notified, don't touch state")
    ap.add_argument("--file", help="parse a saved HTML file instead of fetching")
    args = ap.parse_args()

    try:
        if args.file:
            html = open(args.file, encoding="utf-8").read()
        else:
            html = fetch()
    except Exception as e:
        print(f"Fetch failed: {e}", file=sys.stderr)
        return 1

    all_screenings = parse(html)
    if not all_screenings:
        print("Parsed page but found zero Dune screenings - layout change or film removed?", file=sys.stderr)
        return 3

    eligible = [s for s in all_screenings if s.start.date() >= CUTOFF]
    last = all_screenings[-1].start
    print(f"Dune screenings on page: {len(all_screenings)} "
          f"({all_screenings[0].start:%d.%m.} - {last:%d.%m.%Y}); on/after {CUTOFF}: {len(eligible)}")

    if args.debug:
        for s in eligible:
            print("  ", s.describe())
        return 0

    notified = load_state()
    new = [s for s in eligible if s.key not in notified]
    if not new:
        print("Nothing new.")
        return 0

    if args.dry_run:
        print("Would notify:")
        for s in new:
            print("  ", s.describe())
        return 0

    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set", file=sys.stderr)
        return 2

    notify(new)
    save_state(notified | {s.key for s in new})
    print(f"Notified about {len(new)} new screening(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

import re
import time
from datetime import datetime, timezone

import requests
from markdownify import markdownify

from db import get_checkpoint, set_checkpoint
from models import PressRelease

# The HTML press-release pages (www.justice.gov/news/press-releases) sit
# behind an Akamai bot-challenge that a plain script can't pass — this JSON
# endpoint is unprotected and returns structured data instead.
API_URL = "https://www.justice.gov/api/v1/press_releases.json"
SCRAPER_NAME = "doj_press"

# Confirmed by inspection: results are ordered oldest-first with no working
# sort/search query params (they're silently ignored), so the highest page
# number always holds the newest releases. page=0 returned a 2009 item;
# page=(count // pagesize) returned items dated the day of testing.
PAGE_SIZE = 20
MAX_PAGES_PER_RUN = 25  # backstop so a reset/first run doesn't walk the full ~269k-row archive

KEYWORDS = [
    "ransomware", "cryptocurrency", "crypto", "bitcoin", "virtual currency",
    "digital asset", "money laundering", "sanctions", "ofac", "darknet",
    "cyber", "hacking",
]
KEYWORD_RE = re.compile("|".join(re.escape(k) for k in KEYWORDS), re.IGNORECASE)


def _matched_keywords(text):
    return sorted({m.group(0).lower() for m in KEYWORD_RE.finditer(text)})


def _parse_epoch(value):
    # API embeds a unix timestamp as text inside a <time> tag, e.g.
    # '<time datetime="2025-02-05T12:37:10-05:00">1738777030</time>\n'
    match = re.search(r">(\d+)<", value or "")
    if not match:
        return None
    return datetime.fromtimestamp(int(match.group(1)), tz=timezone.utc)


def _total_count():
    resp = requests.get(API_URL, params={"page": 0}, timeout=30)
    resp.raise_for_status()
    return int(resp.json()["metadata"]["resultset"]["count"])


def _fetch_page(page):
    resp = requests.get(API_URL, params={"page": page}, timeout=30)
    resp.raise_for_status()
    return resp.json()["results"]


def run(session):
    """Walk pages backward from the newest, stopping at the last URL seen on
    a prior run (or after MAX_PAGES_PER_RUN), and store only releases whose
    title/body match a ransomware/crypto/sanctions keyword."""
    total = _total_count()
    last_page = total // PAGE_SIZE
    stop_at_url = get_checkpoint(session, SCRAPER_NAME)

    inserted = 0
    page = last_page
    pages_walked = 0
    newest_url_seen = None

    while page >= 0 and pages_walked < MAX_PAGES_PER_RUN:
        results = _fetch_page(page)
        pages_walked += 1
        if not results:
            break

        hit_checkpoint = False
        for item in results:
            if newest_url_seen is None:
                newest_url_seen = item["url"]
            if stop_at_url and item["url"] == stop_at_url:
                hit_checkpoint = True
                break

            keywords = _matched_keywords(f"{item['title']}\n{item.get('body', '')}")
            if not keywords:
                continue

            if session.query(PressRelease).filter_by(url=item["url"]).one_or_none():
                continue

            session.add(
                PressRelease(
                    source="DOJ",
                    external_id=item.get("uuid"),
                    title=item["title"],
                    url=item["url"],
                    published_at=_parse_epoch(item.get("created")),
                    matched_keywords=",".join(keywords),
                    raw_markdown=markdownify(item.get("body", "")).strip(),
                )
            )
            inserted += 1

        if hit_checkpoint:
            break
        page -= 1
        time.sleep(0.2)  # be polite to a public .gov endpoint

    if newest_url_seen:
        set_checkpoint(session, SCRAPER_NAME, newest_url_seen)
    session.commit()
    print(f"[doj_press] walked {pages_walked} page(s), inserted {inserted} matching releases")

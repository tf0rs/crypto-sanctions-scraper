import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify

from db import get_checkpoint, set_checkpoint
from models import PressRelease

# No JSON:API, no RSS scoped to recent-actions specifically (rss.xml exists
# but is a generic "recently edited pages" feed covering the whole site —
# confirmed its items are static reference pages like "Syria Sanctions",
# sorted by edit date back to 2022, not new designation actions). sitemap.xml
# is the only structured listing, filtered to the /recent-actions/<date>
# pattern.
BASE_URL = "https://ofac.treasury.gov"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
SCRAPER_NAME = "ofac_press"

# URLs look like /recent-actions/20260714 or /recent-actions/20260710_33
# (a numeric suffix for multiple same-day actions) — the date is in the
# slug itself, so sorting the URL strings gives correct chronological order
# without depending on sitemap lastmod (confirmed unreliable on FinCEN's
# sitemap; not re-verified here, but there's no reason to trust it more).
RECENT_ACTION_RE = re.compile(r"/recent-actions/(\d{8})(?:_\d+)?$")

# ofac.treasury.gov sits behind the same slow Akamai edge as fincen.gov
# (confirmed: dur=10101 on a cache HIT for /recent-actions) — not
# bot-protection, just slow.
REQUEST_TIMEOUT = 45
MAX_ACTIONS_PER_RUN = 50

# Deliberately excludes "sanctions", "ofac", and "money laundering" — this
# is OFAC's own site, so "sanctions" and "OFAC" are near-universal the same
# way "money laundering" was on FinCEN's site (see fincen.py), and
# "sanctions" specifically is OFAC's entire reason for existing.
#
# "digital currency" was added after a first test batch (50 pages) matched
# only 1 — suspiciously low next to DOJ's and Europol's hit rates. Checking
# unmatched pages directly found real "Digital Currency Address - XBT
# 1MTndG..." entries (same convention as sdn.py) sitting in the body text
# with nothing else in KEYWORDS to catch them: OFAC's own phrase is "digital
# currency", not "virtual currency" or "cryptocurrency".
KEYWORDS = [
    "ransomware", "cryptocurrency", "crypto", "bitcoin", "virtual currency",
    "digital asset", "digital currency", "darknet", "hacking",
]
# Same crypto/cryptography distinction as doj_press.py/fincen.py/europol.py —
# see those modules for why a plain \bcrypto\b or fully unbounded match are
# both wrong.
_KEYWORD_PARTS = [
    r"\bcrypto(?!graph)" if kw == "crypto" else r"\b" + re.escape(kw) + r"\b"
    for kw in KEYWORDS
]
KEYWORD_RE = re.compile("(?:" + "|".join(_KEYWORD_PARTS) + ")", re.IGNORECASE)


def _matched_keywords(text):
    return sorted({m.group(0).lower() for m in KEYWORD_RE.finditer(text)})


def _action_urls():
    resp = requests.get(SITEMAP_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    urls = set()
    for match in re.finditer(r"<loc>([^<]+)</loc>", resp.text):
        url = match.group(1)
        if RECENT_ACTION_RE.search(url):
            urls.add(url)
    return sorted(urls)  # slug is YYYYMMDD[_N] — lexicographic sort is chronological


def _extract_page(url):
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    title = soup.title.string if soup.title else url
    title = title.replace(" | Office of Foreign Assets Control", "").strip()

    published_at = None
    date_field = soup.select_one(".field--name-field-release-date .field__item")
    if date_field:
        published_at = datetime.strptime(date_field.get_text(strip=True), "%m/%d/%Y")

    # The actual Treasury press release, when one exists, is hosted on a
    # different domain (home.treasury.gov) — this page is often just an
    # index/summary with a link out, not the full statement. Keep the link
    # inline in raw_markdown since there's no separate column for it and
    # it's the closest thing to "the real source" for narrative content.
    press_link = soup.select_one(".field--name-field-press-release-link a")
    press_link_line = ""
    if press_link and press_link.get("href"):
        press_link_line = f"Press release: {urljoin(BASE_URL, press_link['href'])}\n\n"

    # Field machine name is "field_body", not "body" — a plain
    # .field--name-body silently matches unrelated boilerplate elements
    # (page header text, search-widget labels) instead of erroring, which is
    # why this was checked directly against real HTML rather than guessed.
    body_field = soup.select_one(".field--name-field-body")
    body_html = ""
    if body_field:
        label = body_field.select_one(".field__label")
        if label:
            label.decompose()  # visually-hidden "Recent Actions Body" label, not real content
        body_html = str(body_field)

    full_markdown = press_link_line + markdownify(body_html).strip()
    return title, published_at, full_markdown


def run(session):
    """Walk OFAC's recent-actions archive (~3.1k dated entries per
    sitemap.xml, one page per day of designations/updates, sometimes several
    per day) newest-first, stopping at the last URL seen on a prior run —
    same checkpoint-by-URL pattern as doj_press.py. Unlike fincen.py, this
    corpus is too large to store in full, so matched_keywords gates storage
    here, not just tags it."""
    urls = _action_urls()
    stop_at_url = get_checkpoint(session, SCRAPER_NAME)
    # Fetched once up front rather than queried per-item inside the loop —
    # see doj_press.py's identical comment: sources now run concurrently,
    # each on its own connection, and a per-item session.query() mid-loop
    # would trigger autoflush and hold a write transaction open for most of
    # the run instead of just at the final commit.
    existing_urls = {row.url for row in session.query(PressRelease.url).filter_by(source="OFAC_PRESS")}

    inserted = 0
    walked = 0
    newest_url_seen = None

    for url in reversed(urls):  # newest first
        if walked >= MAX_ACTIONS_PER_RUN:
            break
        walked += 1

        if newest_url_seen is None:
            newest_url_seen = url
        if stop_at_url and url == stop_at_url:
            break

        title, published_at, full_markdown = _extract_page(url)
        keywords = _matched_keywords(f"{title}\n{full_markdown}")
        if keywords and url not in existing_urls:
            session.add(
                PressRelease(
                    source="OFAC_PRESS",
                    title=title,
                    url=url,
                    published_at=published_at,
                    matched_keywords=",".join(keywords),
                    raw_markdown=full_markdown,
                )
            )
            existing_urls.add(url)
            inserted += 1

    if newest_url_seen:
        set_checkpoint(session, SCRAPER_NAME, newest_url_seen)
    session.commit()
    print(f"[ofac_press] walked {walked} action(s) ({len(urls)} total known), inserted {inserted} matching")

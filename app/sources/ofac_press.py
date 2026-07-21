import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urljoin

import http_client
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
# bot-protection, just slow (~8-12s per request, regardless of concurrency —
# see FETCH_WORKERS below). A full backfill of the ~3.1k known action pages
# would take ~9 hours fetched sequentially, which is why this was originally
# capped at 50 pages/run (a ~16-day backfill at 4 runs/day). Parallelizing
# fetches (FETCH_WORKERS) cuts wall-clock time per run by roughly that
# factor, which is what makes a much higher per-run cap practical.
REQUEST_TIMEOUT = 45
MAX_ACTIONS_PER_RUN = 500

# Since the bottleneck is pure per-request network/edge latency (not
# something that serializes on OFAC's end — see below), pages are fetched
# concurrently instead of one at a time. 10 was chosen after testing
# directly against ofac.treasury.gov rather than assumed: a 30-request burst
# and a sustained 120-request run at this concurrency both came back 100%
# HTTP 200 with steady ~8-12s per-request timing throughout (no widening,
# no errors) — no sign of rate-limiting or WAF pushback at this level.
# Fetching only (network I/O) is parallelized; keyword matching and all
# database writes stay strictly sequential in run()'s main thread — a
# SQLAlchemy session isn't safe to touch from multiple threads.
FETCH_WORKERS = 10

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
    resp = http_client.get(SITEMAP_URL, timeout=REQUEST_TIMEOUT)
    urls = set()
    for match in re.finditer(r"<loc>([^<]+)</loc>", resp.text):
        url = match.group(1)
        if RECENT_ACTION_RE.search(url):
            urls.add(url)
    return sorted(urls)  # slug is YYYYMMDD[_N] — lexicographic sort is chronological


def _extract_page(url):
    resp = http_client.get(url, timeout=REQUEST_TIMEOUT)
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


def _batch_urls(all_urls, stop_at_url):
    """Which URLs this run should fetch: newest-first, stopping at the last
    URL seen on a prior run (checkpoint) or MAX_ACTIONS_PER_RUN, whichever
    comes first. This only needs the URL list itself (from sitemap.xml), not
    page content, so it's resolved before any fetching happens — letting the
    actual fetches run concurrently as a flat batch instead of one at a time."""
    batch = []
    newest_url_seen = None
    for url in reversed(all_urls):  # newest first
        if len(batch) >= MAX_ACTIONS_PER_RUN:
            break
        if newest_url_seen is None:
            newest_url_seen = url
        if stop_at_url and url == stop_at_url:
            break
        batch.append(url)
    return batch, newest_url_seen


def run(session):
    """Walk OFAC's recent-actions archive (~3.1k dated entries per
    sitemap.xml, one page per day of designations/updates, sometimes several
    per day) newest-first, stopping at the last URL seen on a prior run —
    same checkpoint-by-URL pattern as doj_press.py. Unlike fincen.py, this
    corpus is too large to store in full, so matched_keywords gates storage
    here, not just tags it.

    Page fetches run concurrently (FETCH_WORKERS) since the bottleneck is
    per-request network latency, not anything CPU-bound or database-related.
    Keyword matching and all session writes happen afterward, sequentially,
    in this function's own thread — a SQLAlchemy session isn't safe to touch
    from multiple threads, so nothing DB-related happens inside the pool."""
    urls = _action_urls()
    stop_at_url = get_checkpoint(session, SCRAPER_NAME)
    batch, newest_url_seen = _batch_urls(urls, stop_at_url)

    # Fetched once up front rather than queried per-item inside the loop —
    # see doj_press.py's identical comment: sources now run concurrently,
    # each on its own connection, and a per-item session.query() mid-loop
    # would trigger autoflush and hold a write transaction open for most of
    # the run instead of just at the final commit.
    existing_urls = {row.url for row in session.query(PressRelease.url).filter_by(source="OFAC_PRESS")}

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        # pool.map preserves input order, so zipping against `batch` below
        # lines results back up with their URLs correctly. Consuming it via
        # list() also means the first exception raised by any fetch (after
        # http_client's own retries are exhausted) propagates out of this
        # function immediately, matching the previous sequential version's
        # all-or-nothing failure behavior — a bad page still fails the whole
        # run rather than silently dropping data.
        fetched = list(pool.map(_extract_page, batch))

    inserted = 0
    for url, (title, published_at, full_markdown) in zip(batch, fetched):
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
    print(f"[ofac_press] walked {len(batch)} action(s) ({len(urls)} total known), inserted {inserted} matching")

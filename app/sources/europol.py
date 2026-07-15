import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from markdownify import markdownify

from db import get_checkpoint, set_checkpoint
from models import PressRelease

# The newsroom article pages are a client-rendered React app (same
# Drupal-backend-plus-React-frontend shape as chainabuse.com) — the raw
# HTML is just a loading shell with a meta description, no real body text.
# The actual data API was found by downloading the app's JS bundle
# (/static/js/main.*.js) and grepping for string literals containing "api/"
# — same technique used to find chainabuse's GraphQL endpoint. It turned up
# `l.a.get("api/node?url=".concat(encodeURIComponent(e))...)`, resolved
# against the same /cms/ base that rss.xml and sitemap.xml redirect to.
BASE_URL = "https://www.europol.europa.eu"
NODE_API_URL = f"{BASE_URL}/cms/api/node"
RSS_URL = f"{BASE_URL}/rss.xml"  # redirects to /cms/api/rss/news; only exposes the latest 10 items
SCRAPER_NAME = "europol"

# Each node's JSON response includes navigation.previous/next — a linked
# list through the newsroom's full chronological history. This is used
# instead of the RSS feed for discovery beyond the latest 10 items: start
# from the newest article (from the RSS feed) and walk backward via
# `previous` until the checkpoint URL (last run's newest) is hit.
MAX_ARTICLES_PER_RUN = 100

# Excludes "cyber" and "money laundering", unlike doj_press.py's list.
# Confirmed empirically: with those included, 21 of 48 matches on a 100-
# article test walk were driven by "cyber" or "money laundering" alone, with
# no other keyword hit — including a human-trafficking arrests release, a
# terrorism report, a drug-trafficking corridor story, and a "Fact Check"
# post about Europol's own IT systems. Nearly every Europol release
# mentions "cybercrime" somewhere, because "European Cybercrime Centre
# (EC3)" is boilerplate self-description that appears across unrelated
# press releases, not a signal that the release is actually about
# cybercrime. "sanctions" and "hacking" were left in — each only matched
# once or twice, alongside a genuine crypto/ransomware term, no equivalent
# boilerplate problem found.
KEYWORDS = [
    "ransomware", "cryptocurrency", "crypto", "bitcoin", "virtual currency",
    "digital asset", "sanctions", "darknet", "hacking",
]
# "crypto" needs care: a plain \bcrypto\b misses genuine hits like
# "cryptocurrencies" (plural) and "cryptojacking" because \b requires a
# non-word character immediately after "crypto", which compound/plural
# forms don't have — confirmed losing a real match ("...cryptocurrencies to
# hinder financial tracing...") when a blanket \b...\b was tried first. But
# no boundary at all wrongly matches "cryptography"/"cryptographic"
# (confirmed on this exact data: a "...post-quantum cryptography
# migration..." report got tagged as crypto-relevant purely on that
# substring). A negative lookahead threads the needle: allow any
# continuation except "graph".
_KEYWORD_PARTS = [
    r"\bcrypto(?!graph)" if kw == "crypto" else r"\b" + re.escape(kw) + r"\b"
    for kw in KEYWORDS
]
KEYWORD_RE = re.compile("(?:" + "|".join(_KEYWORD_PARTS) + ")", re.IGNORECASE)


def _matched_keywords(text):
    return sorted({m.group(0).lower() for m in KEYWORD_RE.finditer(text)})


def _fetch_node(path):
    resp = requests.get(NODE_API_URL, params={"url": path}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _latest_path():
    resp = requests.get(RSS_URL, timeout=30)
    resp.raise_for_status()
    match = re.search(r"<item><title>.*?</title><link>([^<]+)</link>", resp.text)
    if not match:
        return None
    return match.group(1).replace(BASE_URL, "")


def run(session):
    stop_at_url = get_checkpoint(session, SCRAPER_NAME)
    path = _latest_path()

    inserted = 0
    walked = 0
    newest_url_seen = None

    while path and walked < MAX_ARTICLES_PER_RUN:
        node = _fetch_node(path)
        walked += 1
        url = BASE_URL + node["alias"]

        if newest_url_seen is None:
            newest_url_seen = url
        if stop_at_url and url == stop_at_url:
            break

        title = node["title"]
        body_html = node.get("body", "")
        keywords = _matched_keywords(f"{title}\n{body_html}")
        if keywords and not session.query(PressRelease).filter_by(url=url).one_or_none():
            session.add(
                PressRelease(
                    source="EUROPOL",
                    external_id=str(node.get("id")),
                    title=title,
                    url=url,
                    published_at=datetime.fromtimestamp(node["published"], tz=timezone.utc),
                    matched_keywords=",".join(keywords),
                    raw_markdown=markdownify(body_html).strip(),
                )
            )
            inserted += 1

        path = node.get("navigation", {}).get("previous")
        time.sleep(0.2)  # be polite to a public EU institution API

    if newest_url_seen:
        set_checkpoint(session, SCRAPER_NAME, newest_url_seen)
    session.commit()
    print(f"[europol] walked {walked} article(s), inserted {inserted} matching")

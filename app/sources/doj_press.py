import html
import re
import time
from datetime import datetime, timezone

import http_client
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

# Layer 1: high-precision crypto, ransomware, sanctions, and hacking terms.
# Any hit here is included unconditionally.
CORE_KEYWORDS = [
    "ransomware", "cryptocurrency", "crypto", "bitcoin", "virtual currency",
    "digital asset", "sanctions", "ofac", "darknet", "hacking",
]
# Tag-only / broad keywords: recorded in `matched_keywords` when a release is
# included, but NOT sufficient on their own. Confirmed empirically across 1,100
# DOJ releases: standalone "money laundering" (457 rows) pulled in unrelated
# COVID/healthcare/cartel fraud, and standalone "cyber" (326 rows) pulled in
# 177 CSAM cases (via NCMEC "CyberTipline"), cyberstalking, and unrelated cases
# citing a prosecutor's "Cybercrime Unit" boilerplate.
TAG_KEYWORDS = ["money laundering", "cyber"]
KEYWORDS = CORE_KEYWORDS + TAG_KEYWORDS

# Layer 2 (for `cyber`-only hits): exclude CSAM / cyberstalking noise and
# require at least one concrete cyber-threat / cyber-fraud signal so
# prosecutor unit boilerplate alone doesn't trigger inclusion.
CYBER_EXCLUDE_RE = re.compile(
    r"cybertipline|cybertip|cyberstalk|child sexual|child pornography|"
    r"project safe childhood|exploitation of children",
    re.IGNORECASE,
)
CYBER_SIGNAL_RE = re.compile(
    r"\b(?:"
    r"cyberattack|cyber-attack|cyber espionage|cyberespionage|cyber intrusion|"
    r"cyber-enabled|cybercrime marketplace|cybersecurity fraud|"
    r"malware|botnet|phishing|ddos|data breach|computer intrusion|"
    r"computer fraud|unauthorized access|account takeover|account-takeover|"
    r"sim swap|carding|remote it worker"
    r")\b",
    re.IGNORECASE,
)


def _compile_keyword_re(terms):
    # "crypto" uses a negative lookahead to match "cryptocurrencies"/
    # "cryptojacking" without hitting "cryptography". "cyber" excludes
    # "cybertip*" and "cyberstalk*" while still matching "cybercrime"/
    # "cybersecurity"/"cyberattack".
    parts = []
    for kw in terms:
        if kw == "crypto":
            parts.append(r"\bcrypto(?!graph)")
        elif kw == "cyber":
            parts.append(r"\bcyber(?!tip|stalk)")
        else:
            parts.append(r"\b" + re.escape(kw) + r"\b")
    return re.compile("(?:" + "|".join(parts) + ")", re.IGNORECASE)


CORE_KEYWORD_RE = _compile_keyword_re(CORE_KEYWORDS)
KEYWORD_RE = _compile_keyword_re(KEYWORDS)


def _matched_keywords(text):
    if CORE_KEYWORD_RE.search(text):
        return sorted({m.group(0).lower() for m in KEYWORD_RE.finditer(text)})
    # Layer 2: allow cyber-only releases only when corroborated by a concrete
    # cyber-threat signal and not CSAM/cyberstalking.
    if (
        re.search(r"\bcyber(?!tip|stalk)", text, re.IGNORECASE)
        and not CYBER_EXCLUDE_RE.search(text)
        and CYBER_SIGNAL_RE.search(text)
    ):
        return sorted({m.group(0).lower() for m in KEYWORD_RE.finditer(text)})
    return []


def _parse_epoch(value):
    # API embeds a unix timestamp as text inside a <time> tag, e.g.
    # '<time datetime="2025-02-05T12:37:10-05:00">1738777030</time>\n'
    match = re.search(r">(\d+)<", value or "")
    if not match:
        return None
    return datetime.fromtimestamp(int(match.group(1)), tz=timezone.utc)


def _total_count():
    resp = http_client.get(API_URL, params={"page": 0}, timeout=30)
    return int(resp.json()["metadata"]["resultset"]["count"])


def _fetch_page(page):
    resp = http_client.get(API_URL, params={"page": page}, timeout=30)
    return resp.json()["results"]


def run(session):
    """Walk pages backward from the newest, stopping at the last URL seen on
    a prior run (or after MAX_PAGES_PER_RUN), and store only releases whose
    title/body match a ransomware/crypto/sanctions keyword."""
    total = _total_count()
    last_page = total // PAGE_SIZE
    stop_at_url = get_checkpoint(session, SCRAPER_NAME)
    # Fetched once up front rather than queried per-item inside the loop:
    # sources now run concurrently (see scraper.py), each on its own
    # session/connection, and a per-item session.query() mid-loop triggers
    # SQLAlchemy's autoflush — which would flush pending inserts and hold a
    # write transaction open for most of the run instead of just at the
    # final commit, risking other sources' commits blocking on this one.
    existing_urls = {row.url for row in session.query(PressRelease.url).filter_by(source="DOJ")}

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

            title = html.unescape(item["title"])
            keywords = _matched_keywords(f"{title}\n{item.get('body', '')}")
            if not keywords:
                continue

            if item["url"] in existing_urls:
                continue

            session.add(
                PressRelease(
                    source="DOJ",
                    external_id=item.get("uuid"),
                    title=title,
                    url=item["url"],
                    published_at=_parse_epoch(item.get("created")),
                    matched_keywords=",".join(keywords),
                    raw_markdown=markdownify(item.get("body", "")).strip(),
                )
            )
            existing_urls.add(item["url"])
            inserted += 1

        if hit_checkpoint:
            break
        page -= 1
        time.sleep(0.2)  # be polite to a public .gov endpoint

    if newest_url_seen:
        set_checkpoint(session, SCRAPER_NAME, newest_url_seen)
    session.commit()
    print(f"[doj_press] walked {pages_walked} page(s), inserted {inserted} matching releases")

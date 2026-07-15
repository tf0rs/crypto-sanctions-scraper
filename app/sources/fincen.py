import io
import re
from datetime import datetime
from urllib.parse import urljoin

import http_client
from bs4 import BeautifulSoup
from pypdf import PdfReader

from models import PressRelease

# No JSON:API (404), no RSS/feed (404) — confirmed by probing. sitemap.xml
# is the only structured listing available, but it's a genuine sitemap
# (every page on the site), not an advisories-specific export, so it has to
# be filtered down to the /resources/advisories/ prefix.
BASE_URL = "https://www.fincen.gov"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
ADVISORY_PREFIX = f"{BASE_URL}/resources/advisories/"
SCRAPER_NAME = "fincen"

# FinCEN's edge takes ~8-10s to respond even on a cache hit (confirmed by
# testing, server-timing header showed dur=8000 on a HIT) — not
# bot-protection, just slow. Use a generous timeout, not a short one that'll
# misread slowness as unreachable.
REQUEST_TIMEOUT = 45
# Each advisory costs two slow requests (HTML page + its PDF), so a full
# backfill of the ~194-item list is deliberately spread across several
# 6-hourly runs instead of done in one long invocation.
MAX_ADVISORIES_PER_RUN = 30

# Not used as a storage gate (see run() — every advisory is stored
# regardless), only to populate matched_keywords for later querying.
# Deliberately excludes "money laundering", "sanctions", "cyber" — those are
# near-universal across FinCEN's entire corpus (it's a financial-crimes
# agency) and wouldn't discriminate anything even as a tag. Confirmed
# empirically: an earlier version used those terms as an inclusion filter
# and matched 28/30 advisories checked, every one only via "money
# laundering" or "sanctions" — including several 2007-2009 "Advisory
# Withdrawal" notices, years before Bitcoin existed.
KEYWORDS = [
    "ransomware", "cryptocurrency", "crypto", "bitcoin", "virtual currency",
    "convertible virtual currency", "digital asset", "darknet",
]
# "crypto" needs care, not a plain \bcrypto\b: that misses genuine hits like
# "cryptocurrencies" (plural) and "cryptojacking" (\b requires a non-word
# char right after "crypto", which those don't have) while a fully unbounded
# match wrongly hits "cryptography"/"cryptographic". Both failure modes were
# confirmed on europol.py's data, which shares this exact keyword-matching
# approach. A negative lookahead threads the needle: allow any continuation
# except "graph".
_KEYWORD_PARTS = [
    r"\bcrypto(?!graph)" if kw == "crypto" else r"\b" + re.escape(kw) + r"\b"
    for kw in KEYWORDS
]
KEYWORD_RE = re.compile("(?:" + "|".join(_KEYWORD_PARTS) + ")", re.IGNORECASE)


def _matched_keywords(text):
    return sorted({m.group(0).lower() for m in KEYWORD_RE.finditer(text)})


def _advisory_urls():
    resp = http_client.get(SITEMAP_URL, timeout=REQUEST_TIMEOUT)
    pattern = rf"<loc>({re.escape(ADVISORY_PREFIX)}[^<]+)</loc>"
    return sorted(set(re.findall(pattern, resp.text)))


def _extract_page(url):
    resp = http_client.get(url, timeout=REQUEST_TIMEOUT)
    soup = BeautifulSoup(resp.text, "html.parser")

    title = soup.title.string if soup.title else url
    title = title.replace(" | FinCEN.gov", "").strip()

    published_at = None
    date_field = soup.select_one(".field--name-field-date-release time[datetime]")
    if date_field:
        published_at = datetime.fromisoformat(date_field["datetime"].replace("Z", "+00:00"))

    # Confirmed on every advisory checked: the HTML body field contains only
    # a logo image, and the actual advisory text is a linked PDF attachment.
    # Body text is still extracted as a fallback in case some entries differ.
    body_field = soup.select_one(".field--name-body")
    body_text = body_field.get_text(" ", strip=True) if body_field else ""

    pdf_text = ""
    pdf_link = soup.select_one('a[href$=".pdf"]')
    if pdf_link:
        pdf_url = urljoin(BASE_URL, pdf_link["href"])
        pdf_resp = http_client.get(pdf_url, timeout=REQUEST_TIMEOUT)
        reader = PdfReader(io.BytesIO(pdf_resp.content))
        pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)

    full_text = "\n\n".join(t for t in (body_text, pdf_text) if t)
    return title, published_at, full_text


def run(session):
    """Store every FinCEN advisory, not just crypto/ransomware-matching
    ones. The full corpus is only ~194 items total (confirmed via
    sitemap.xml) — small enough that filtering at ingestion isn't worth the
    risk of silently dropping something relevant (see the KEYWORDS comment
    above for how that nearly went wrong already). matched_keywords is
    still computed and stored so the crypto/ransomware-specific subset stays
    a queryable slice, not a storage decision."""
    urls = _advisory_urls()
    already_stored = {
        row.url for row in session.query(PressRelease.url).filter_by(source="FINCEN")
    }
    todo = [u for u in urls if u not in already_stored][:MAX_ADVISORIES_PER_RUN]

    inserted = 0
    for url in todo:
        title, published_at, full_text = _extract_page(url)
        keywords = _matched_keywords(f"{title}\n{full_text}")
        session.add(
            PressRelease(
                source="FINCEN",
                title=title,
                url=url,
                published_at=published_at,
                matched_keywords=",".join(keywords),
                raw_markdown=full_text,
            )
        )
        inserted += 1

    session.commit()
    print(f"[fincen] stored {inserted} new advisories ({len(urls)} total known, {len(already_stored)} already stored)")

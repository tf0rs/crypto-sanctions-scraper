# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Scrapes government sources for crypto-linked sanctions designations and
ransomware/crypto enforcement news, on a 6-hour GitHub Actions cron, and
commits the results to a SQLite file (`data/sanctions.db`) checked into this
repo. No servers, no external DB — the workflow is the only runtime, and git
history doubles as an audit trail of what changed each run.

## Commands

Run the scraper locally:
```bash
cd app
uv sync
uv run python scraper.py
```

Dependencies are managed with `uv` (`app/pyproject.toml` + `app/uv.lock`) —
run `uv add <package>` to add one rather than editing `pyproject.toml` by
hand, so the lockfile stays in sync. `uv sync --locked` (used in CI) fails
loudly if the lockfile drifts from `pyproject.toml` instead of silently
re-resolving.

Trigger a run manually instead of waiting for cron:
```bash
gh workflow run scrape.yml --repo <owner>/crypto-sanctions-scraper
```

## Architecture

**OFAC's SDN list embeds crypto wallet addresses as free text inside the
Remarks column, not as a structured field.** The CSV
(`https://www.treasury.gov/ofac/downloads/sdn.csv`, no header row, columns
documented in `app/sources/sdn.py`) has clauses like `Digital Currency
Address - ETH 0x098B71...; alt. Digital Currency Address - ETH 0xa0e1c8...`
mixed in with other remarks (DOB, aliases, program info). `sdn.py` parses
these with a regex over semicolon-split segments rather than treating the
column as structured data. Only entities with at least one parsed address are
stored — the full list is ~19k rows and the vast majority have no crypto
exposure, which isn't the point of this project.

**`justice.gov`'s HTML pages are behind an Akamai bot-challenge that returns a
JS interstitial instead of content to any non-browser client** (confirmed:
plain `curl`/`requests` gets a 5-second-refresh meta tag and a proof-of-work
challenge script, HTTP 200 either way so status code alone doesn't reveal
this). `doj_press.py` avoids the whole page by hitting
`https://www.justice.gov/api/v1/press_releases.json` instead, which is
unauthenticated and returns clean JSON — found by testing several path
guesses (`/rss.xml`, `/api/v1/press_releases.json`, etc.) until one returned
real content instead of the interstitial. If any other DOJ-adjacent source
gets added later, check for an equivalent JSON/RSS endpoint before assuming
HTML scraping is required.

**The DOJ API's pagination has no working sort or search parameters — `sort`,
`keys`, and similar guesses are silently ignored** — but results are stably
ordered oldest-first by insertion, confirmed by checking `page=0` (a 2009
item) against `page=(total_count // page_size)` (items dated the day of
testing). `doj_press.py` exploits this: it computes the last page from the
API's own `count` field and walks backward, so it never needs a working sort
param. This also means the "last page" boundary shifts by however many items
were added since the previous run — the walk handles that by matching on item
URL against a stored checkpoint rather than assuming a fixed page number.

**Checkpointing is a single `scraper_checkpoints` table keyed by scraper
name**, storing an opaque value whose meaning is source-specific (for
`doj_press`, the newest press release URL seen last run). `MAX_PAGES_PER_RUN`
in `doj_press.py` is a safety cap so a cleared/first-run checkpoint doesn't
walk the entire ~269k-row DOJ archive in one invocation.

**Storage is SQLite, not Postgres/Supabase, because this project has no
Supabase project set up and the data volume is small** (thousands of rows,
not millions) — a single committed file is simpler than provisioning and
paying for a hosted DB. The tradeoff: `git diff` on the binary `.db` file is
unreadable, unlike a flat-file/CSV approach would be. If per-run diffability
becomes important, consider having the workflow also emit a JSON snapshot
purely for that purpose, without changing `.db` as the source of truth.

**All seven sources are implemented and run concurrently** (`scraper.py`,
via `ThreadPoolExecutor`), each on its own SQLite connection — SQLAlchemy
sessions aren't thread-safe to share. Most of a source's wall-clock time is
spent waiting on slow upstream HTTP responses (fincen.gov and
ofac.treasury.gov in particular sit behind the same slow Akamai edge config,
~10-12s per request even on a cache hit — confirmed via `server-timing`
response headers, not bot-protection), which threads overlap fine even
though SQLite only allows one actual writer at a time. `db.py` sets WAL mode
plus a 60s busy timeout as a safety net for that.

**Before enabling concurrency, `doj_press.py`, `europol.py`, and
`ofac_press.py` had a latent bug that would have caused intermittent lock
contention or failures**: each ran a per-item `session.query(...)`
existence-check inside its loop, which triggers SQLAlchemy's autoflush and
would hold a write transaction open for most of a run (up to ~10 minutes for
`ofac_press`) instead of just at the final commit. Fixed by fetching known
URLs into a set once up front instead — the pattern `fincen.py` already used
for an unrelated reason (avoiding per-item DB round-trips).

**All HTTP requests go through `app/http_client.py`, a thin wrapper adding
retries via `stamina`** (`attempts=3` on `requests.exceptions.RequestException`,
which covers connection failures, timeouts, and `raise_for_status()`'s 5xx
errors alike). Added after a transient DNS resolution failure for
`ofsistorage.blob.core.windows.net` (`uk_ofsi.py`'s source) took down an
entire scrape run with zero actual bug involved — that source running
standalone and succeeding moments later confirmed it was a one-off network
blip, not a real problem, but the run still shouldn't have failed over it.
Note the module name: it's deliberately not `http.py` — this project imports
flatly from a single `app/` directory on `sys.path` (no package-relative
imports), so a local `http.py` would shadow the stdlib `http` package that
`requests`/`urllib3` depend on internally, breaking every source at once.

**`eu_sanctions.py` and `europol.py` needed their real data APIs
reverse-engineered, not guessed.** Europol's newsroom pages are a
client-rendered React app (same shape as chainabuse.com) — the real content
API (`cms/api/node?url=<path>`) was found by downloading the site's JS
bundle and grepping for `api/` string literals, the same technique used for
chainabuse-scraper's GraphQL endpoint. The EU consolidated sanctions list's
download URL requires a `token` query param that looks session-minted but
isn't — decoding it gives the literal string `token-2017`, a static value
published directly in the EU Open Data Portal's own DCAT API
(`data.europa.eu/api/hub/search/datasets/...`), not something requiring a
webgate.ec.europa.eu visit first as originally assumed.

**Keyword-matching bugs, found empirically, not by inspection**: an early
version of `fincen.py` and `europol.py`'s keyword lists included generic
terms ("money laundering", "cyber", "sanctions") that turned out to be
near-universal in each source's own corpus — FinCEN is a financial-crimes
agency, so "money laundering" matched almost everything; Europol's
boilerplate self-description ("European Cybercrime Centre (EC3)") appears on
unrelated releases. Both lists were tightened after checking actual matched
titles, not just trusting a plausible-looking keyword set. Separately, a
plain `\bcrypto\b` word-boundary "fix" for a `cryptography`
false-positive was tried and reverted — it also silently dropped genuine
matches like "cryptocurrencies" (plural) and "cryptojacking", since `\b`
requires a non-word character immediately after "crypto" that those don't
have. The actual fix is a negative lookahead (`crypto(?!graph)`), used
identically across `doj_press.py`, `fincen.py`, and `europol.py`.

**`ofac_press.py` and `fincen.py` are both sitemap-driven crawls of a large
dated-URL archive, not paginated APIs** — neither site has a
recent-actions/advisories-specific feed (their generic `rss.xml` feeds are
"recently edited pages" across the whole site, unrelated to designation
dates). `sitemap.xml` gives the full URL list instead. OFAC's URLs encode
the date in the slug itself (`/recent-actions/YYYYMMDD[_N]`), so sorting the
URL strings gives correct chronological order for free; FinCEN's sitemap
`lastmod` values were checked and found unreliable (clustered around a
single site-wide redeploy date, not real advisory dates), which is why
`fincen.py` stores every advisory rather than trying to detect "new" ones by
date.

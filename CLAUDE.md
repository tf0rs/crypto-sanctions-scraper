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
pip install -r requirements.txt
python scraper.py
```

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

**Five of the seven planned sources are stubbed, not implemented** —
`ofac_press.py`, `fincen.py`, `eu_sanctions.py`, `uk_ofsi.py`, `europol.py`.
Each module's docstring records what was confirmed about that source during
initial research (reachability, auth requirements, data format) so
implementing it doesn't start from zero. Notably: OFAC recent-actions and
FinCEN advisories both connected but didn't respond within 10s during
testing (unconfirmed whether that's bot-protection or just slow — recheck
from an actual Actions run); the EU consolidated list requires a session
token minted by its own webgate page rather than a static download URL; UK
OFSI's list is a plain, unauthenticated CSV and is the easiest of the five to
finish next.

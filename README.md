# crypto-sanctions-scraper

Scrapes US and European government sources for crypto-linked sanctions
designations and ransomware/crypto-related enforcement news, and stores them
in a SQLite database committed to this repo.

## What's tracked

- **Sanctioned entities with crypto exposure** — from OFAC's SDN list, filtered
  to entities that have at least one digital currency address on file (e.g.
  Lazarus Group, CYBER2-program individuals).
- **Press releases** — from DOJ (and eventually FinCEN, OFAC, Europol) filtered
  to those mentioning ransomware, cryptocurrency, sanctions, etc. Each stored
  release keeps its source URL and full body converted to markdown.

## Status

All seven sources are implemented and run concurrently (`app/scraper.py`):
`sdn.py`, `uk_ofsi.py`, and `eu_sanctions.py` extract crypto-linked
sanctioned entities; `doj_press.py`, `fincen.py`, `ofac_press.py`, and
`europol.py` extract matching press releases/advisories. Each module's
comments document what was confirmed about that source during
development — several turned out to have no public API/feed and needed
reverse-engineering (an internal JSON endpoint for Europol, a sitemap-driven
crawl for FinCEN/OFAC), and a few keyword-matching bugs were found and
fixed along the way (see git history for specifics).

## Running locally

```bash
cd app
uv sync
uv run python scraper.py
```

This creates/updates `data/sanctions.db` (SQLite). No environment variables
or external services required — set `DB_PATH` to point elsewhere if needed.

## Automation

`.github/workflows/scrape.yml` runs the scraper every 6 hours and commits
`data/sanctions.db` back to the repo if it changed. Trigger manually with:

```bash
gh workflow run scrape.yml --repo <owner>/crypto-sanctions-scraper
```

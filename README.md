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

Two sources are fully implemented: `app/sources/sdn.py` (OFAC SDN list) and
`app/sources/doj_press.py` (DOJ press releases). The rest —
`ofac_press.py`, `fincen.py`, `eu_sanctions.py`, `uk_ofsi.py`, `europol.py` —
are stubs; each module's docstring notes what was confirmed about that
source's endpoint during initial research and what's still unverified.

## Running locally

```bash
cd app
pip install -r requirements.txt
python scraper.py
```

This creates/updates `data/sanctions.db` (SQLite). No environment variables
or external services required — set `DB_PATH` to point elsewhere if needed.

## Automation

`.github/workflows/scrape.yml` runs the scraper every 6 hours and commits
`data/sanctions.db` back to the repo if it changed. Trigger manually with:

```bash
gh workflow run scrape.yml --repo <owner>/crypto-sanctions-scraper
```

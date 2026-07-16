# Progress checkpoint — 2026-07-16

Repo: https://github.com/tf0rs/crypto-sanctions-scraper (public, pushed, live)
Local: `~/develop/crypto-sanctions-scraper`
Latest local change: `sdn.py` rewritten to fix the address-undercount issue
(see below) — not yet committed as of this checkpoint.

## What this project is

Scrapes US/EU government sources for crypto-linked sanctions designations
and ransomware/crypto enforcement news. Stores results in SQLite
(`data/sanctions.db`), committed to the repo by a GitHub Actions cron
(every 6 hours). No external DB, no servers.

## Status: all 7 sources implemented and verified working

| Source | Populates | Last known count | Notes |
|---|---|---|---|
| `sdn.py` | sanctioned_entities + crypto_addresses | 90 entities, 942 addresses | Fixed address-undercount issue — see below |
| `uk_ofsi.py` | same | 2 entities (Garantex, Ayyash/Gaza Now) | |
| `eu_sanctions.py` | same | 0 entities | Confirmed empirically: this source has zero crypto/wallet data, checked exhaustively |
| `doj_press.py` | press_releases | 53+ | Keyword-filtered, checkpoint-based incremental walk |
| `fincen.py` | press_releases | 60+ (of 194 total) | Stores ALL advisories, not keyword-filtered (see rationale in file) — backfill in progress |
| `ofac_press.py` | press_releases | 5+ (of ~3,074 total) | Keyword-filtered, checkpoint-based; backfill in progress (~50/run, ~16 days to catch up) |
| `europol.py` | press_releases | 25+ | Keyword-filtered, walks `navigation.previous` linked list |

## Infrastructure

- **Parallel execution**: all 7 sources run concurrently via `ThreadPoolExecutor`
  in `scraper.py`, each on its own SQLite connection (WAL mode + 60s busy
  timeout in `db.py`).
- **HTTP retries**: `app/http_client.py` wraps all outbound requests with
  `stamina`-based retries (3 attempts), added after a transient DNS failure
  took down a full run.
- **Dependency management**: `uv` (`app/pyproject.toml` + `app/uv.lock`),
  not pip/requirements.txt.
- **CI**: `.github/workflows/scrape.yml`, runs every 6 hours + manual
  `workflow_dispatch`. Commits `data/sanctions.db` with the full run output
  (per-source counts + a DB summary) as the commit message body. Verified
  working in real GitHub Actions (first manual run: `9m51s`, succeeded,
  commit `fc36b9e`).

## Key discoveries/bugs found this session (see git log for full detail)

1. **OFSI regex was format-specific and missed a real entity** (AYASH/Gaza
   Now) — fixed by generalizing to a ticker-allowlist + address-shape regex
   instead of anchoring on OFAC's exact phrasing.
2. **FinCEN/Europol keyword lists had near-universal terms** ("money
   laundering", "cyber", "sanctions") causing false-positive matches on
   boilerplate — tightened after checking actual matched titles.
3. **`crypto` substring bug**: matched inside "cryptography" — fixed with a
   negative lookahead (`crypto(?!graph)`), not a blanket `\b...\b` (which
   would have also dropped genuine matches like "cryptocurrencies" and
   "cryptojacking").
4. **OFAC's own site has no useful RSS/API for recent-actions** — discovery
   is via `sitemap.xml`, date-sortable via the URL slug itself
   (`/recent-actions/YYYYMMDD`).
5. **Missing "digital currency" keyword** in `ofac_press.py` — OFAC's own
   phrase differs from "virtual currency"/"cryptocurrency", found via a
   suspiciously low 1/50 match rate.
6. **Europol and EU sanctions APIs needed reverse-engineering**: Europol's
   real content API (`cms/api/node?url=<path>`) was found by grepping the
   site's JS bundle (same technique as chainabuse-scraper's GraphQL
   discovery). The EU sanctions list's download token looked session-minted
   but is a static public value (`token-2017`) published in the EU Open
   Data Portal's own DCAT API.
7. **Autoflush/write-lock risk before parallelizing**: `doj_press.py`,
   `europol.py`, `ofac_press.py` each did a per-item `session.query()`
   inside their loops, which would've held a write transaction open for
   most of a run once sources ran concurrently. Fixed by fetching known
   URLs into a set up front instead.

## ✅ Resolved — sdn.py address-undercount issue

Was flagged as an open issue in the previous checkpoint (found by
cross-checking `sdn.py`'s CSV-sourced data against `ofac_press.py`'s
website-sourced data for ISIL KHORASAN: 9 addresses via CSV vs 134 via the
OFAC website). Follow-up investigation and fix, in order:

1. **Scoped the problem first.** OFAC's `sdn.csv` caps its Remarks field at
   exactly 1000 characters (confirmed: max length across all ~19k rows is
   exactly 1000, 33 rows hit it exactly). Cross-referencing that cap against
   the 90 crypto-exposed entities found **23 of them (over a quarter) hit
   this cap**, including LAZARUS GROUP and GARANTEX — not a one-off, a
   systemic issue.
2. **Found OFAC's own structured, untruncated export**:
   `SDN_ADVANCED.XML` (via `sanctionslistservice.ofac.treas.gov/api/
   PublicationPreview/exports/SDN_ADVANCED.XML`), where each address is a
   `Feature` element rather than a shared text blob — nothing to truncate.
   Cross-checked entity-level detection between the old CSV-substring
   method and the new XML-feature method across the *entire* file: exactly
   90 entities either way, zero discrepancy — confirming the CSV's cap
   distorted address *counts*, not *which* entities got flagged as
   crypto-exposed at all.
3. **Rewrote `sdn.py`** to keep using `sdn.csv` for entity metadata
   (name/entity_type/program/remarks) but source addresses from
   `SDN_ADVANCED.XML` instead, parsed via streaming `iterparse` (the file is
   ~126MB). Chain tickers are read from the XML's own `FeatureType`
   reference table rather than a hardcoded list, which also surfaced 4
   chains (`BSV`, `XRP`, `ARB`, `BSC`) the old hardcoded ticker list used
   elsewhere (`uk_ofsi.py`, `eu_sanctions.py`) doesn't cover.
4. **Verified the fix**: total addresses went from 460 to 942 on the same
   90 entities; zero suspiciously-short (<15 char) addresses remain (the old
   CSV bug's telltale sign); ISIL KHORASAN now shows exactly 134, matching
   the independent press-release cross-check. Added ~62s to `sdn.py`'s
   runtime (mostly the XML download) — negligible against the ~10min budget
   already used by the slowest sources.

**Status**: code change made and tested locally (scratch DB), not yet
committed/pushed as of this checkpoint. `CLAUDE.md` updated with full
details. Still need to: run the complete 7-source scraper once more against
the real `data/sanctions.db`, verify, then commit + push.

## Other known limitations (lower priority, already accepted)

- FinCEN/OFAC backfills are intentionally capped per run (~30/~50 items)
  given those hosts take ~10-12s/request — will finish gradually over the
  next ~2 days (FinCEN) / ~16 days (OFAC) of 6-hourly runs.
- No automated test suite — all verification was manual, against live data,
  during development.
- No cross-referencing yet between `press_releases` and
  `sanctioned_entities`/`crypto_addresses` (e.g. flagging when a press
  release mentions an address that's also in the SDN list) — mentioned
  early on as a natural next step, not built.
- SQLite-as-committed-binary tradeoff: `git diff` on `data/sanctions.db` is
  unreadable. Accepted tradeoff for a small-scale solo project; revisit if
  it becomes annoying (see `CLAUDE.md`).

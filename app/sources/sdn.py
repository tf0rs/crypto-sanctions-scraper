import csv
import io
import xml.etree.ElementTree as ET

import http_client
from models import CryptoAddress, SanctionedEntity

# Redirects (302) to a signed S3 URL on sanctionslistservice.ofac.treas.gov —
# `requests` follows redirects by default, so this stable public URL is the
# right one to hit rather than the S3 link itself.
SDN_CSV_URL = "https://www.treasury.gov/ofac/downloads/sdn.csv"

# No header row in the source file. Column order confirmed by inspection:
# ent_num, SDN_Name, SDN_Type, Program, Title, Call_Sign, Vess_type, Tonnage,
# GRT, Vess_flag, Vess_owner, Remarks.
COLUMNS = [
    "ent_num", "name", "entity_type", "program", "title", "call_sign",
    "vess_type", "tonnage", "grt", "vess_flag", "vess_owner", "remarks",
]

# No stable treasury.gov alias for this file the way sdn.csv has one — this
# is the documented API path directly. Confirmed reachable via `requests`
# (302 -> signed S3 URL, same redirect shape as the CSV); a `curl` failure
# hitting this same host during development turned out to be a local
# sandbox TLS cert-store quirk, not a real connectivity problem.
SDN_ADVANCED_XML_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ADVANCED.XML"
XML_NS = "{https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ADVANCED_XML}"

# Digital currency addresses are NOT parsed from the CSV's Remarks column —
# see the git history for sdn.py (pre-XML-migration version) for that
# approach and why it was replaced. In short: OFAC's own published CSV caps
# the Remarks field at exactly 1000 characters (confirmed: max length across
# all ~19k rows is exactly 1000, with 33 rows hitting it exactly). For
# entities with long address lists this doesn't just clip the last address —
# it can drop the large majority of them. Confirmed on ISIL KHORASAN: the CSV
# only yields 9 addresses (regex-parsed from truncated Remarks text), while
# the untruncated SDN_ADVANCED_XML export has 134 for the same entity
# (matching OFAC's own July 2026 designation-update announcement page
# exactly, scraped independently by ofac_press.py). 23 of the 90 SDN entities
# with crypto exposure hit this exact 1000-char cap, so this wasn't an edge
# case — it affects over a quarter of the entities this project tracks,
# including LAZARUS GROUP and GARANTEX.
#
# The XML has no such cap: it's Feature elements (one per address, not a
# shared text blob), so there's nothing to truncate. Entity-level detection
# (which entities have ANY crypto exposure) was cross-checked between the
# CSV-substring approach and the XML-feature approach across the full file
# and the two matched exactly (90 entities either way, zero discrepancy) —
# so the CSV's Remarks cap distorts *address counts* per entity, not *which*
# entities get flagged as crypto-exposed in the first place.


def _clean(value):
    value = value.strip()
    return "" if value == "-0-" else value


def fetch_rows():
    """CSV rows are still used for entity metadata (name/entity_type/
    program/remarks) — only address extraction moved to the XML export."""
    resp = http_client.get(SDN_CSV_URL, timeout=60)
    reader = csv.reader(io.StringIO(resp.text))
    for row in reader:
        if len(row) < len(COLUMNS):
            continue
        yield dict(zip(COLUMNS, (_clean(v) for v in row)))


def fetch_digital_currency_addresses():
    """Returns {ent_num: [(chain, address), ...]}, parsed from the
    SDN_ADVANCED.XML export. Chain tickers are read from the file's own
    FeatureType reference table (any entry whose label starts with "Digital
    Currency Address - ") rather than a hardcoded ticker list, so a chain
    OFAC adds in the future is picked up automatically instead of silently
    being skipped. Confirmed 19 chains present as of this writing, including
    several (BSV, XRP, ARB, BSC) that a hand-maintained ticker allowlist
    would have missed — the CSV-based approach used a fixed list precisely
    because the CSV gave no structured way to discover chains dynamically.

    Uses iterparse + elem.clear() rather than loading the ~126MB file into
    an in-memory tree — this file is roughly 20x the size of the CSV it
    replaces for address data.
    """
    resp = http_client.get(SDN_ADVANCED_XML_URL, timeout=120)

    chain_by_feature_type = {}
    entities = {}

    context = ET.iterparse(io.BytesIO(resp.content), events=("end",))
    for _event, elem in context:
        if elem.tag == XML_NS + "FeatureType":
            label = elem.text or ""
            if label.startswith("Digital Currency Address - "):
                chain_by_feature_type[elem.get("ID")] = label.removeprefix("Digital Currency Address - ")
            elem.clear()

        elif elem.tag == XML_NS + "DistinctParty":
            ent_num = elem.get("FixedRef")
            addresses = []
            for feature in elem.iter(XML_NS + "Feature"):
                chain = chain_by_feature_type.get(feature.get("FeatureTypeID"))
                if chain is None:
                    continue
                for detail in feature.iter(XML_NS + "VersionDetail"):
                    if detail.text:
                        addresses.append((chain, detail.text))
            if addresses:
                entities[ent_num] = addresses
            elem.clear()

    return entities


def run(session):
    """Ingest SDN entries. Only entities with at least one digital currency
    address are stored — the full SDN list is ~19k rows and the vast
    majority carry no crypto exposure, which is the specific slice of the
    list this project tracks."""
    addresses_by_entity = fetch_digital_currency_addresses()

    seen = 0
    upserted = 0
    for row in fetch_rows():
        seen += 1
        addresses = addresses_by_entity.get(row["ent_num"])
        if not addresses:
            continue

        entity = (
            session.query(SanctionedEntity)
            .filter_by(source="OFAC_SDN", source_id=row["ent_num"])
            .one_or_none()
        )
        if entity is None:
            entity = SanctionedEntity(source="OFAC_SDN", source_id=row["ent_num"], name=row["name"])
            session.add(entity)
            session.flush()  # need entity.id to attach CryptoAddress rows
        entity.name = row["name"]
        entity.entity_type = row["entity_type"]
        entity.programs = row["program"]
        entity.remarks = row["remarks"]

        # Full sync against the XML's address list, not just an append —
        # rows inserted by the pre-XML, CSV-regex version of this module are
        # still sitting in the table (e.g. SUEX OTC's address list included
        # a truncated fragment like "1B64QRxf", 8 characters, from the CSV's
        # 1000-char Remarks cap) and won't go away on their own, since the
        # old code only ever added addresses, never removed any. Any stored
        # address not present in this run's authoritative XML list — whether
        # it's old truncated garbage or an address OFAC has since delisted —
        # gets removed here.
        #
        # `addresses` (from the XML) is deduplicated into a dict keyed by
        # address before use, not iterated as-is — OFAC's own XML contains a
        # small number of genuine duplicate Feature entries within a single
        # entity (confirmed: 956 raw entries vs 942 unique (entity, address)
        # pairs across the full file). An earlier version of this loop
        # iterated `addresses` directly and checked membership against a
        # snapshot dict that was never updated as new rows were added within
        # the loop — for an entity with a duplicate entry, both occurrences
        # passed the "not already stored" check and both got session.add()'d,
        # violating the (entity_id, address) unique constraint at flush time.
        # This only surfaced on a genuinely empty database — every prior test
        # happened to run against a DB that already had that address from an
        # earlier run, which masked the bug (the address was already in
        # `existing_by_address`, so the duplicate never reached session.add()
        # twice). Deduplicating up front removes the possibility entirely,
        # regardless of what state the database starts in.
        target_by_address = {}
        for chain, address in addresses:
            target_by_address.setdefault(address, chain)

        existing_by_address = {a.address: a for a in entity.addresses}

        for stale_address, stale_row in existing_by_address.items():
            if stale_address not in target_by_address:
                session.delete(stale_row)

        for address, chain in target_by_address.items():
            if address not in existing_by_address:
                session.add(CryptoAddress(entity_id=entity.id, address=address, chain=chain))

        upserted += 1

    session.commit()
    print(f"[sdn] scanned {seen} SDN rows, upserted {upserted} entities with crypto addresses")

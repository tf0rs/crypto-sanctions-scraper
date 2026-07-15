import csv
import io
import re

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

# Crypto wallet addresses aren't a separate structured field — OFAC embeds
# them as free-text clauses inside Remarks, e.g.:
#   "...; Digital Currency Address - ETH 0x098B71...; alt. Digital Currency
#   Address - ETH 0xa0e1c8...; Secondary sanctions risk: ..."
# Confirmed present on real entries (LAZARUS GROUP has 8 ETH addresses this
# way; several CYBER2-program individuals have single XBT addresses).
DIGITAL_CURRENCY_RE = re.compile(r"Digital Currency Address\s*-\s*(\S+)\s+(\S+)")


def _clean(value):
    value = value.strip()
    return "" if value == "-0-" else value


def _parse_addresses(remarks):
    addresses = []
    for segment in remarks.split(";"):
        match = DIGITAL_CURRENCY_RE.search(segment)
        if match:
            chain, address = match.group(1), match.group(2).rstrip(".")
            addresses.append((chain, address))
    return addresses


def fetch_rows():
    resp = http_client.get(SDN_CSV_URL, timeout=60)
    reader = csv.reader(io.StringIO(resp.text))
    for row in reader:
        if len(row) < len(COLUMNS):
            continue
        yield dict(zip(COLUMNS, (_clean(v) for v in row)))


def run(session):
    """Ingest SDN entries. Only entities with at least one digital currency
    address are stored — the full SDN list is ~19k rows and the vast
    majority carry no crypto exposure, which is the specific slice of the
    list this project tracks."""
    seen = 0
    upserted = 0
    for row in fetch_rows():
        seen += 1
        addresses = _parse_addresses(row["remarks"])
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

        existing = {a.address for a in entity.addresses}
        for chain, address in addresses:
            if address not in existing:
                session.add(CryptoAddress(entity_id=entity.id, address=address, chain=chain))
                existing.add(address)

        upserted += 1

    session.commit()
    print(f"[sdn] scanned {seen} SDN rows, upserted {upserted} entities with crypto addresses")

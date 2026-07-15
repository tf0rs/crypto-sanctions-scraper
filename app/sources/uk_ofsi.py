import csv
import io
import re

import http_client
from models import CryptoAddress, SanctionedEntity

# Confirmed stable, unauthenticated, no token required.
OFSI_CSV_URL = "https://ofsistorage.blob.core.windows.net/publishlive/2022format/ConList.csv"

# Row 1 is a "Last Updated,<date>" banner line, not part of the table —
# the real header is row 2. Key columns: "Name 6" holds the full name for
# Entity-type rows (e.g. "GARANTEX EUROPE OU"); Name 1-5 are individual name
# parts (title/forename/middle/surname/suffix) and are empty for entities.
# "Group ID" is the true entity identifier — OFSI emits one row per
# name/alias variant, so multiple rows can share a Group ID.
NAME_PARTS = ["Name 1", "Name 2", "Name 3", "Name 4", "Name 5"]

# Unlike OFAC's SDN remarks (one consistent "Digital Currency Address - X Y"
# convention throughout), OFSI's "Other Information" is genuinely free-text
# prose with no single convention — confirmed two different ones in the same
# file:
#   "Digital Currency Address: XBT 3Lpoy53K625..."             (GARANTEX EUROPE OU)
#   "(1) ETH: 0x175d4445...  (2) BNB: 0x175d4445...  (10) BTC: 3Q8H2ZW..."  (AYASH / Gaza Now)
# A regex anchored on the "Digital Currency Address" label — the first
# format I tried — silently missed the second entity entirely. Instead this
# searches for any known chain ticker followed by an address-shaped token
# (>=20 contiguous alnum chars covers both 0x... hex and base58), regardless
# of surrounding label text. Ticker list is every chain confirmed present in
# either OFAC's SDN remarks or OFSI's Other Information field during
# research, not a guess — extend it if a run turns up an unrecognized chain
# next to what looks like a missed address.
KNOWN_CHAIN_TICKERS = [
    "BCH", "BNB", "BTC", "BTG", "DASH", "DOGE", "ETC", "ETH", "LTC", "SOL",
    "TRX", "USDC", "USDT", "XBT", "XMR", "XVG", "ZEC",
]
DIGITAL_CURRENCY_RE = re.compile(
    r"\b(" + "|".join(KNOWN_CHAIN_TICKERS) + r")\b\s*:?\s*([A-Za-z0-9]{20,64})"
)


def _entity_name(row):
    name6 = row.get("Name 6", "").strip()
    if name6:
        return name6
    return " ".join(p for p in (row.get(col, "").strip() for col in NAME_PARTS) if p)


def _parse_addresses(other_info):
    addresses = []
    for match in DIGITAL_CURRENCY_RE.finditer(other_info):
        chain, address = match.group(1), match.group(2).rstrip(".")
        addresses.append((chain, address))
    return addresses


def fetch_rows():
    resp = http_client.get(OFSI_CSV_URL, timeout=60)
    text = resp.content.decode("utf-8-sig")
    _banner, header_and_rows = text.split("\n", 1)
    reader = csv.DictReader(io.StringIO(header_and_rows))
    yield from reader


def run(session):
    """Ingest OFSI consolidated list entries, grouped by Group ID (OFSI
    emits one row per name/alias variant). Only groups with at least one
    parsed digital currency address are stored, same rationale as sdn.py:
    the full list is ~20k rows and crypto exposure is the specific slice
    this project tracks."""
    groups = {}  # group_id -> accumulated data
    seen_rows = 0

    for row in fetch_rows():
        seen_rows += 1
        group_id = row.get("Group ID", "").strip()
        if not group_id:
            continue

        other_info = row.get("Other Information", "")
        addresses = _parse_addresses(other_info)
        group = groups.setdefault(group_id, {"addresses": {}})
        if addresses:
            group["name"] = _entity_name(row)
            group["entity_type"] = row.get("Group Type", "").strip()
            group["programs"] = row.get("Regime", "").strip()
            group["remarks"] = other_info
            for chain, address in addresses:
                group["addresses"][address] = chain

    upserted = 0
    for group_id, group in groups.items():
        if not group["addresses"]:
            continue

        entity = (
            session.query(SanctionedEntity)
            .filter_by(source="UK_OFSI", source_id=group_id)
            .one_or_none()
        )
        if entity is None:
            entity = SanctionedEntity(source="UK_OFSI", source_id=group_id, name=group["name"])
            session.add(entity)
            session.flush()  # need entity.id to attach CryptoAddress rows
        entity.name = group["name"]
        entity.entity_type = group["entity_type"]
        entity.programs = group["programs"]
        entity.remarks = group["remarks"]

        existing = {a.address for a in entity.addresses}
        for address, chain in group["addresses"].items():
            if address not in existing:
                session.add(CryptoAddress(entity_id=entity.id, address=address, chain=chain))
                existing.add(address)

        upserted += 1

    session.commit()
    print(f"[uk_ofsi] scanned {seen_rows} OFSI rows, upserted {upserted} entities with crypto addresses")

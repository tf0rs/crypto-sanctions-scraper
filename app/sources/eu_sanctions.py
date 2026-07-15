import csv
import io
import re

import http_client
from models import CryptoAddress, SanctionedEntity

# Found via the EU Open Data Portal's own DCAT API — fetching
# https://data.europa.eu/api/hub/search/datasets/consolidated-list-of-persons-groups-and-entities-subject-to-eu-financial-sanctions
# lists this exact URL as the dataset's official CSV distribution. The
# earlier stub assumed the "token" param was a per-session value minted by
# visiting the webgate.ec.europa.eu page first (a guessed placeholder token
# got a 500). It isn't — decoding it (`token=dG9rZW4tMjAxNw`) gives the
# literal string "token-2017": a static, permanently public token embedded
# directly in the EU's own published catalog metadata, not a session
# artifact.
EU_CSV_URL = "https://webgate.ec.europa.eu/fsd/fsf/public/files/csvFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw"

# Semicolon-delimited, one row per name/alias variant (confirmed: LogicalId
# 13 spans 4 rows, one per alias of "Saddam Hussein Al-Tikriti") — same
# shape as OFSI's list. Entity_LogicalId is the true per-entity identifier
# to group on.
REMARK_COLUMNS = [
    "Entity_Remark", "NameAlias_Remark", "Address_Remark",
    "BirthDate_Remark", "Identification_Remark", "Citizenship_Remark",
]

# Same ticker-allowlist + address-shape heuristic as uk_ofsi.py, reused
# because OFAC's and OFSI's crypto-address conventions both turned out to
# be inconsistent free text rather than one fixed format — a general
# pattern generalizes better than a label-specific regex. Confirmed
# empirically that this currently matches ZERO rows across the EU list's
# ~37k entries: checked every Remark-style column above, and separately
# ran a bare unlabeled address-shape scan (0x-hex and base58, no ticker
# required) across the entire raw 21MB file. The EU consolidated list does
# not track crypto wallet addresses at all as of this writing, unlike
# OFAC/OFSI. Kept as a real implementation anyway (not a stub) so this
# starts working automatically if the EU ever adds this data.
KNOWN_CHAIN_TICKERS = [
    "BCH", "BNB", "BTC", "BTG", "DASH", "DOGE", "ETC", "ETH", "LTC", "SOL",
    "TRX", "USDC", "USDT", "XBT", "XMR", "XVG", "ZEC",
]
DIGITAL_CURRENCY_RE = re.compile(
    r"\b(" + "|".join(KNOWN_CHAIN_TICKERS) + r")\b\s*:?\s*([A-Za-z0-9]{20,64})"
)


def _entity_name(row):
    whole = (row.get("NameAlias_WholeName") or "").strip()
    if whole:
        return whole
    parts = [row.get("NameAlias_FirstName"), row.get("NameAlias_MiddleName"), row.get("NameAlias_LastName")]
    return " ".join(p.strip() for p in parts if p and p.strip())


def _parse_addresses(row):
    addresses = []
    for col in REMARK_COLUMNS:
        text = row.get(col) or ""
        for match in DIGITAL_CURRENCY_RE.finditer(text):
            chain, address = match.group(1), match.group(2).rstrip(".")
            addresses.append((chain, address))
    return addresses


def fetch_rows():
    resp = http_client.get(EU_CSV_URL, timeout=60)
    text = resp.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    yield from reader


def run(session):
    """Ingest EU consolidated list entries, grouped by Entity_LogicalId
    (one row per name/alias variant, like OFSI). Only entities with at
    least one parsed digital currency address are stored — see the
    KNOWN_CHAIN_TICKERS comment above for why this currently matches
    nothing."""
    groups = {}  # entity_id -> accumulated data
    seen_rows = 0

    for row in fetch_rows():
        seen_rows += 1
        entity_id = (row.get("Entity_LogicalId") or "").strip()
        if not entity_id:
            continue

        addresses = _parse_addresses(row)
        group = groups.setdefault(entity_id, {"addresses": {}})
        if addresses:
            group["name"] = _entity_name(row)
            group["entity_type"] = (row.get("Entity_SubjectType") or "").strip()
            group["programs"] = (row.get("Entity_Regulation_Programme") or "").strip()
            group["remarks"] = (row.get("Entity_Remark") or "").strip()
            for chain, address in addresses:
                group["addresses"][address] = chain

    upserted = 0
    for entity_id, group in groups.items():
        if not group["addresses"]:
            continue

        entity = (
            session.query(SanctionedEntity)
            .filter_by(source="EU_CONSOLIDATED", source_id=entity_id)
            .one_or_none()
        )
        if entity is None:
            entity = SanctionedEntity(source="EU_CONSOLIDATED", source_id=entity_id, name=group["name"])
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
    print(f"[eu_sanctions] scanned {seen_rows} EU sanctions rows, upserted {upserted} entities with crypto addresses")

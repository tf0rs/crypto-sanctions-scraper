"""
STUB — not yet implemented.

Target: UK OFSI Consolidated List, confirmed reachable as a direct CSV
download (HTTP 200 during testing, no auth/token needed):
https://ofsistorage.blob.core.windows.net/publishlive/2022format/ConList.csv

This is the most straightforward of the stubbed sources — no bot protection,
no token, plain CSV. Columns weren't inspected yet; check for a wallet/crypto
identifier field or free-text remarks field (OFAC embeds crypto addresses in
free text — see sdn.py — OFSI's format may differ).
"""


def run(session):
    print("[uk_ofsi] stub — not yet implemented, skipping")

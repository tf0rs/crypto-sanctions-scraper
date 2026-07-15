"""
STUB — not yet implemented.

Target: EU Consolidated Financial Sanctions List, published via the "Financial
Sanctions Files" service at webgate.ec.europa.eu/fsd/fsf. The full-list XML
download requires a session token minted by that page (a URL of the form
.../fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=<TOKEN>) — a
guessed/placeholder token returned HTTP 500 during testing, confirming the
token is real and per-session, not a static value. Implementing this means
first fetching https://webgate.ec.europa.eu/fsd/fsf/public/files to obtain a
live token (same "find it by inspecting the real client" approach used for
chainabuse-scraper's undocumented GraphQL endpoint), then downloading the XML.

Schema-wise, entries are less structured than OFAC's for crypto: the EU list
doesn't have a consistent "Digital Currency Address" convention in the source
data the way OFAC's Remarks field does, so this may need per-entry text
scanning for wallet-looking strings rather than a clean regex.
"""


def run(session):
    print("[eu_sanctions] stub — not yet implemented, skipping")

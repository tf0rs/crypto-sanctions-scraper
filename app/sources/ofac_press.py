"""
STUB — not yet implemented.

Target: https://ofac.treasury.gov/recent-actions — HTML list of new
designation actions, each linking to a press release/notice. This is the
narrative counterpart to sdn.py (which only has the structured SDN entries
themselves).

A direct request to this URL connected but did not return a response within
10s during testing — likely bot-detection or a slow render, not confirmed
either way. Verify reachability from a real GitHub Actions run before
building this out; if it's Akamai-gated like justice.gov, check for a
JSON/RSS endpoint first (see doj_press.py for that pattern working around
the same problem on DOJ's site).
"""


def run(session):
    print("[ofac_press] stub — not yet implemented, skipping")

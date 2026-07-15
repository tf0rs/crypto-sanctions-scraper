import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import func

from db import get_session
from models import CryptoAddress, PressRelease, SanctionedEntity
from sources import doj_press, eu_sanctions, europol, fincen, ofac_press, sdn, uk_ofsi

SOURCES = [sdn, doj_press, ofac_press, fincen, eu_sanctions, uk_ofsi, europol]


def _print_summary(session):
    # This becomes the GitHub Actions commit message body (see scrape.yml) —
    # the point is that `git log` alone tells you the DB's shape over time
    # without needing to check out a binary sqlite file to see it.
    print()
    print("=== Database summary ===")

    entity_counts = (
        session.query(SanctionedEntity.source, func.count())
        .group_by(SanctionedEntity.source)
        .all()
    )
    print(f"sanctioned_entities: {sum(count for _, count in entity_counts)}")
    for source, count in sorted(entity_counts):
        print(f"  {source}: {count}")

    print(f"crypto_addresses: {session.query(CryptoAddress).count()}")

    release_counts = (
        session.query(PressRelease.source, func.count())
        .group_by(PressRelease.source)
        .all()
    )
    print(f"press_releases: {sum(count for _, count in release_counts)}")
    for source, count in sorted(release_counts):
        print(f"  {source}: {count}")


def _run_source(source):
    name = source.__name__.rsplit(".", 1)[-1]
    # Each source gets its own session/connection so they can run
    # concurrently — SQLAlchemy sessions aren't thread-safe to share. Most
    # of a source's wall-clock time is spent waiting on slow upstream HTTP
    # responses (fincen.gov/ofac.treasury.gov in particular: ~10s/request),
    # which threads overlap fine even though sqlite still only allows one
    # actual writer at a time (see db.py's WAL mode + busy timeout).
    session = get_session()
    try:
        source.run(session)
    except Exception:
        session.rollback()
        print(f"[{name}] failed", file=sys.stderr)
        raise
    finally:
        session.close()


def main():
    # A bootstrap session runs Base.metadata.create_all() once before any
    # thread touches the file — on a brand new (table-less) db, two threads
    # racing create_all at the same instant could otherwise both try to
    # create the same table.
    get_session().close()

    errors = []
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        futures = {pool.submit(_run_source, source): source for source in SOURCES}
        for future in as_completed(futures):
            exc = future.exception()
            if exc:
                errors.append(exc)

    summary_session = get_session()
    _print_summary(summary_session)
    summary_session.close()

    if errors:
        raise errors[0]


if __name__ == "__main__":
    main()

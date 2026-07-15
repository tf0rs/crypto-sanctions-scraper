import sys

from db import get_session
from sources import doj_press, eu_sanctions, europol, fincen, ofac_press, sdn, uk_ofsi

SOURCES = [sdn, doj_press, ofac_press, fincen, eu_sanctions, uk_ofsi, europol]


def main():
    session = get_session()
    for source in SOURCES:
        name = source.__name__.rsplit(".", 1)[-1]
        try:
            source.run(session)
        except Exception:
            session.rollback()
            print(f"[{name}] failed", file=sys.stderr)
            raise
    session.close()


if __name__ == "__main__":
    main()

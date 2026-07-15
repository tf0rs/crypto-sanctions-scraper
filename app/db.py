import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base

# Lives outside app/ so the GitHub Actions job can `git add`/commit it as the
# repo's persisted state — there is no external database for this project.
DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "sanctions.db")
DB_PATH = os.environ.get("DB_PATH", DEFAULT_DB_PATH)


def get_session():
    # Sources now run concurrently (see scraper.py), each with its own
    # connection to the same file. SQLite only allows one writer at a time
    # regardless of WAL mode, so a generous busy timeout matters here — the
    # default is 5s, easily exceeded if two sources' commits happen to
    # overlap. WAL mode itself is what lets a slow writer not block fast
    # readers/other sources from making progress in the meantime.
    engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"timeout": 60})
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def get_checkpoint(session, scraper_name):
    from models import ScraperCheckpoint

    row = session.get(ScraperCheckpoint, scraper_name)
    return row.checkpoint if row else None


def set_checkpoint(session, scraper_name, value):
    from models import ScraperCheckpoint

    row = session.get(ScraperCheckpoint, scraper_name)
    if row is None:
        row = ScraperCheckpoint(scraper_name=scraper_name)
        session.add(row)
    row.checkpoint = value

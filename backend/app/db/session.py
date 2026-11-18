from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import settings

_sqlite = settings.database_url.startswith("sqlite")
connect_args = {"check_same_thread": False, "timeout": 30} if _sqlite else {}

# Sync endpoints run on a 40-thread pool; size the connection pool to match so
# bursts of requests (e.g. a whole class logging in) queue for threads, not for
# connections. pre_ping drops connections the database closed while idle.
engine = create_engine(settings.database_url, connect_args=connect_args,
                       pool_size=10, max_overflow=30, pool_pre_ping=True)

if _sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        # WAL lets readers proceed while a write is in progress (SQLite otherwise
        # serialises everything behind one lock); busy_timeout makes writers wait
        # for the lock instead of failing under concurrent load.
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

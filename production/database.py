"""Database engine factory shared by server and portable desktop runtime."""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url


def is_sqlite_url(url: str) -> bool:
    return make_url(url).get_backend_name() == 'sqlite'


def create_database_engine(url: str) -> Engine:
    """Create an engine with safe defaults for PostgreSQL and local SQLite.

    The portable desktop application is deliberately a single-user runtime.  It
    uses SQLite in WAL mode, while service deployments retain PostgreSQL and
    its normal connection-pool behaviour.
    """
    sqlite = is_sqlite_url(url)
    engine = create_engine(
        url,
        pool_pre_ping=not sqlite,
        connect_args={'check_same_thread': False, 'timeout': 15} if sqlite else {},
    )
    if sqlite:
        @event.listens_for(engine, 'connect')
        def configure_sqlite(connection, _record):
            cursor = connection.cursor()
            cursor.execute('PRAGMA journal_mode=WAL')
            cursor.execute('PRAGMA busy_timeout=15000')
            cursor.execute('PRAGMA foreign_keys=ON')
            cursor.close()
    return engine

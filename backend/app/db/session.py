"""SQLAlchemy engine and session factory for GoLogs.

Single-file SQLite, zero-ops, matching PRD §9's rejection of Postgres for
this single-analyst use case. `DB_URL` is fully configurable (PRD §15) so
tests can point at `sqlite:///:memory:` without touching this module.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.models import Base


def make_engine(db_url: str) -> Engine:
    """Create the SQLAlchemy engine for `db_url`.

    In-memory SQLite (`sqlite:///:memory:`) needs `StaticPool` explicitly:
    without it, every new connection checked out of the pool opens a
    brand-new, empty `:memory:` database rather than sharing the one
    `init_db()` already created tables on. This matters in practice, not
    just in theory — FastAPI's `BackgroundTasks` runs synchronous task
    functions in a separate worker thread (`starlette.concurrency.
    run_in_threadpool`), which checks out its own connection; without
    `StaticPool` that connection lands in a different, table-less
    `:memory:` database, producing `OperationalError: no such table`
    despite `init_db()` having already run successfully on the "main"
    connection. File-based SQLite doesn't have this problem (all
    connections open the same file), so the pool override is scoped to
    `:memory:` URLs only.
    """
    is_in_memory_sqlite = db_url.startswith("sqlite") and ":memory:" in db_url
    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    if is_in_memory_sqlite:
        return create_engine(db_url, connect_args=connect_args, poolclass=StaticPool)
    return create_engine(db_url, connect_args=connect_args)


def init_db(engine: Engine) -> None:
    """Create all tables. Idempotent — safe to call on every startup."""
    Base.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Provide a transactional scope: commits on success, rolls back on error."""
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

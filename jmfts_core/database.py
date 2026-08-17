"""JMFTS Database Connection"""

import threading

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session, DeclarativeBase
from contextlib import contextmanager
from typing import Generator

from jmfts_core.config import get_settings


class Base(DeclarativeBase):
    """SQLAlchemy declarative base"""
    pass


# Create engine (lazy initialization). The lock makes first-touch double-checked
# so a concurrent burst (e.g. FastAPI's threadpool serving the first requests, or
# several embedded units starting at once) can't build two engines / two pools.
# See ROADMAP "Concurrency & thread safety", Axis A #3.
#
# Must be an RLock, not a plain Lock: get_session_factory() acquires it and then
# calls get_engine() while still holding it, and get_engine() re-acquires the same
# lock. A non-reentrant Lock self-deadlocks there on the first DB touch of a process
# whose entry point is get_session()/get_session_factory() (e.g. the BEIR loader),
# rather than get_engine() first.
_engine = None
_SessionLocal = None
_init_lock = threading.RLock()


def get_engine():
    global _engine
    if _engine is None:
        with _init_lock:
            if _engine is None:
                settings = get_settings()
                _engine = create_engine(
                    settings.database_url,
                    pool_pre_ping=True,
                    pool_size=5,
                    max_overflow=10,
                )
    return _engine


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        with _init_lock:
            if _SessionLocal is None:
                _SessionLocal = sessionmaker(
                    bind=get_engine(),
                    autocommit=False,
                    autoflush=False,
                )
    return _SessionLocal


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Context manager for database sessions"""
    SessionLocal = get_session_factory()
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """Dependency for FastAPI endpoints.

    NOTE: the commit below runs in the dependency's teardown, which FastAPI
    executes AFTER the response has been sent. A client that acts on the
    response immediately (e.g. create then embed) can therefore race the
    commit. Write endpoints must call ``db.commit()`` themselves before
    returning; this teardown commit is only a backstop.
    """
    SessionLocal = get_session_factory()
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

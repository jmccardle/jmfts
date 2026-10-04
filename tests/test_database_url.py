"""The database URL names its driver.

``postgresql://`` means "SQLAlchemy's default driver", which was psycopg2 through 2.0 and is
psycopg 3 from 2.1. This project declares ``psycopg2-binary`` and leaves ``sqlalchemy``
unbounded above, so an install that resolved 2.1.x failed its first connection with
``No module named 'psycopg'``. Both tests fail on that URL under SQLAlchemy 2.1.
"""

from __future__ import annotations

from sqlalchemy.engine import make_url

from jmfts_core.config import Settings


def test_database_url_names_the_driver_this_project_declares():
    url = make_url(Settings(db_password="p@ss/word").database_url)

    assert url.drivername == "postgresql+psycopg2"
    assert url.password == "p@ss/word"


def test_database_url_driver_imports_in_this_environment():
    url = make_url(Settings().database_url)

    url.get_dialect().import_dbapi()

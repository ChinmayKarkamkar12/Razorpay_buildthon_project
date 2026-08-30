"""Shared fixtures. DB-touching tests are opt-in: they truncate and reseed the
database, so they only run when RUN_DB_TESTS=1 is set in the environment.
"""

import os

import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get("RUN_DB_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="set RUN_DB_TESTS=1 to run (mutates the database)")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def db_conn():
    from src.db import connect

    conn = connect()
    conn.autocommit = True
    yield conn
    conn.close()

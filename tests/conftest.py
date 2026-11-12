import os
import shutil
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from football import config, pipeline                     # noqa: E402
from football.warehouse import SQLiteWarehouse            # noqa: E402
from fake_fotmob import FakeFotMob                        # noqa: E402

NOW = datetime(2026, 10, 18, 6, 0)                        # Sunday morning, 10 rounds played
FIRST_SEASON = 2023


@pytest.fixture(autouse=True)
def short_history(monkeypatch):
    monkeypatch.setattr(config, "FIRST_SEASON", FIRST_SEASON)


@pytest.fixture
def wh(tmp_path):
    w = SQLiteWarehouse(str(tmp_path / "test.db"))
    w.ensure()
    return w


@pytest.fixture(scope="session")
def full_load(tmp_path_factory):
    """A warehouse after a first complete run on 18 October 2026 (built once, copied per test)."""
    path = str(tmp_path_factory.mktemp("full") / "full.db")
    old, config.FIRST_SEASON = config.FIRST_SEASON, FIRST_SEASON
    try:
        w = SQLiteWarehouse(path)
        w.ensure()
        client = FakeFotMob(NOW)
        summary = pipeline.update(w, client, now=NOW, log=lambda *_: None)
        w.con.close()
    finally:
        config.FIRST_SEASON = old
    return path, client, summary


@pytest.fixture
def loaded(full_load, tmp_path):
    path, client, summary = full_load
    copy = str(tmp_path / "loaded.db")
    shutil.copy(path, copy)
    return SQLiteWarehouse(copy), client, summary

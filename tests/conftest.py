import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from football import config, pipeline                     # noqa: E402
from football.warehouse import SQLiteWarehouse            # noqa: E402
from fake_understat import FakeUnderstat                  # noqa: E402

NOW = datetime(2026, 10, 18, 6, 0)                        # Sunday morning, 10 rounds played


@pytest.fixture(autouse=True)
def short_history(monkeypatch):
    monkeypatch.setattr(config, "FIRST_SEASON", 2023)


@pytest.fixture
def wh(tmp_path):
    w = SQLiteWarehouse(str(tmp_path / "test.db"))
    w.ensure()
    return w


@pytest.fixture
def loaded(wh):
    """A warehouse after a first full run on 18 October 2026."""
    client = FakeUnderstat(NOW)
    summary = pipeline.update(wh, client, now=NOW, log=lambda *_: None)
    return wh, client, summary

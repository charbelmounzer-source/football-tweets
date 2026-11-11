"""The BigQuery backend can't be run here (no project in the tests). This checks, against a
stand-in for the client library, that it builds the statements and parameters it should.
The real thing is exercised by the first `python -m football status` on your project."""

import sys
import types
from datetime import date, datetime

import pytest


class Recorder:
    def __init__(self):
        self.queries, self.loads, self.created, self.deleted = [], [], [], []


@pytest.fixture
def bq(monkeypatch):
    rec = Recorder()
    mod = types.ModuleType("google.cloud.bigquery")

    class Simple:
        def __init__(self, *a, **k):
            self.args, self.kwargs = a, k
            self.__dict__.update(k)

    class Job:
        def __init__(self, rows=()):
            self.rows = rows

        def result(self):
            return [types.SimpleNamespace(items=lambda r=r: r.items()) for r in self.rows]

    class Client:
        def __init__(self, project=None, location=None):
            self.project = project

        def create_dataset(self, ds, exists_ok=False):
            rec.created.append(("dataset", ds.args[0]))

        def get_table(self, tid):
            raise NotFound(tid)

        def create_table(self, t):
            rec.created.append(("table", t.args[0], t.clustering_fields))

        def query(self, sql, job_config=None):
            rec.queries.append((sql, job_config.kwargs.get("query_parameters") if job_config else None))
            return Job([{"match_id": 1, "date": date(2026, 10, 17),
                         "kickoff_utc": datetime(2026, 10, 17, 14, 0)}] if sql.startswith("SELECT") else ())

        def load_table_from_json(self, rows, tid, job_config=None):
            rec.loads.append((tid, rows, job_config.kwargs["write_disposition"]))
            return Job()

        def delete_table(self, tid, not_found_ok=False):
            rec.deleted.append(tid)

    class NotFound(Exception):
        pass

    class Table(Simple):
        clustering_fields = None

    for name in ("SchemaField", "Dataset", "LoadJobConfig", "QueryJobConfig",
                 "ArrayQueryParameter", "ScalarQueryParameter"):
        setattr(mod, name, type(name, (Simple,), {}))
    mod.Table, mod.Client = Table, Client
    exc = types.ModuleType("google.api_core.exceptions")
    exc.NotFound = NotFound
    for name, m in (("google", types.ModuleType("google")), ("google.cloud", types.ModuleType("google.cloud")),
                    ("google.cloud.bigquery", mod), ("google.api_core", types.ModuleType("google.api_core")),
                    ("google.api_core.exceptions", exc)):
        monkeypatch.setitem(sys.modules, name, m)
    sys.modules["google.cloud"].bigquery = mod
    from football.warehouse import BigQueryWarehouse
    return BigQueryWarehouse("proj"), rec


def test_ensure_creates_datasets_tables_views(bq):
    wh, rec = bq
    wh.ensure()
    assert ("dataset", "proj.football_core") in rec.created
    assert ("table", "proj.football_core.shots", ["season", "match_id"]) in rec.created
    assert sum(1 for q, _ in rec.queries if q.startswith("CREATE OR REPLACE VIEW")) == 3


def test_replace_is_one_transaction_through_a_staging_table(bq):
    wh, rec = bq
    wh.replace("core.shots", [{"shot_id": 1, "match_id": 7}], {"match_id": [7, 8]})
    tid, rows, disposition = rec.loads[0]
    assert tid == "proj.football_core.shots__staging" and disposition == "WRITE_TRUNCATE"
    assert rows[0]["shot_id"] == 1 and "xg" in rows[0]                 # every column sent
    sql, params = rec.queries[0]
    assert sql.startswith("BEGIN TRANSACTION;") and sql.endswith("COMMIT TRANSACTION;")
    assert "DELETE FROM `proj.football_core.shots` WHERE `match_id` IN UNNEST(@p0);" in sql
    assert "INSERT INTO `proj.football_core.shots`" in sql
    assert params[0].args == ("p0", "INT64", [7, 8])
    assert rec.deleted == ["proj.football_core.shots__staging"]


def test_read_returns_plain_strings_and_typed_parameters(bq):
    wh, rec = bq
    rows = wh.read("tweets.drafts", ["fact_id"], {"run_date": (">=", "2026-11-01")})
    assert rows == [{"match_id": 1, "date": "2026-10-17", "kickoff_utc": "2026-10-17 14:00:00"}]
    sql, params = rec.queries[0]
    assert sql == "SELECT `fact_id` FROM `proj.football_tweets.drafts` WHERE `run_date` >= @p0"
    assert params[0].args == ("p0", "DATE", date(2026, 11, 1))

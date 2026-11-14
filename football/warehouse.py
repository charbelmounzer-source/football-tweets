"""Where the data lives. Two interchangeable backends with the same four operations:

    ensure()                         create datasets / tables, add new columns
    replace(table, rows, where)      delete the rows matching `where`, insert `rows` (one transaction)
    append(table, rows)              insert rows
    read(table, columns, where)      rows as dicts

BigQueryWarehouse is production. SQLiteWarehouse is for tests and for trying things locally
without touching BigQuery. All transformation happens in Python (model.py), so neither
backend needs dialect-specific SQL.

`where` is a dict: {"season": 2026} (equals), {"match_id": [1, 2, 3]} (in a list),
{"run_date": (">=", "2026-11-01")} (comparison). {} means every row.
"""

import sqlite3
from datetime import date, datetime

from . import config
from .schemas import CLUSTERING, DESCRIPTIONS, TABLES, col_type, columns

OPS = {"=", "!=", ">", ">=", "<", "<="}


def _split_where(where):
    """-> list of (column, op, value) with op 'in' for lists."""
    out = []
    for col, v in (where or {}).items():
        if isinstance(v, (list, set, frozenset)):
            out.append((col, "in", sorted(v)))
        elif isinstance(v, tuple):
            if v[0] not in OPS:
                raise ValueError(f"unknown operator {v[0]!r}")
            out.append((col, v[0], v[1]))
        else:
            out.append((col, "=", v))
    return out


class Warehouse:
    def ensure(self):
        raise NotImplementedError

    def replace(self, table, rows, where):
        raise NotImplementedError

    def append(self, table, rows):
        raise NotImplementedError

    def read(self, table, columns=None, where=None):
        raise NotImplementedError

    def usage(self):
        """One line on what this session cost the backend, or None when nothing is billed."""
        return None


# ---------------------------------------------------------------------------- SQLite
SQLITE_TYPES = {"INT64": "INTEGER", "FLOAT64": "REAL"}       # everything else is TEXT
CHUNK = 500                                                   # ids per IN (...) list


class SQLiteWarehouse(Warehouse):
    def __init__(self, path):
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row

    @staticmethod
    def _name(table):
        return table.replace(".", "_")

    def ensure(self):
        for table, cols in TABLES.items():
            name = self._name(table)
            ddl = ", ".join(f'"{c}" {SQLITE_TYPES.get(t, "TEXT")}' for c, t in cols)
            self.con.execute(f'CREATE TABLE IF NOT EXISTS "{name}" ({ddl})')
            have = {r[1] for r in self.con.execute(f'PRAGMA table_info("{name}")')}
            for c, t in cols:
                if c not in have:
                    self.con.execute(f'ALTER TABLE "{name}" ADD COLUMN "{c}" {SQLITE_TYPES.get(t, "TEXT")}')
        self.con.commit()

    def _conds(self, where):
        """-> list of (sql, params) alternatives; several when an IN list is chunked."""
        parts, params, in_part = [], [], None
        for col, op, v in _split_where(where):
            if op == "in":
                if in_part is not None:
                    raise ValueError("only one list condition per call")
                in_part = (col, v)
            else:
                parts.append(f'"{col}" {op} ?')
                params.append(v)
        if in_part is None:
            return [(" AND ".join(parts) or "1=1", params)]
        col, values = in_part
        out = []
        for i in range(0, len(values), CHUNK):
            chunk = values[i:i + CHUNK]
            cond = parts + [f'"{col}" IN ({",".join("?" * len(chunk))})']
            out.append((" AND ".join(cond), params + chunk))
        return out                                           # empty list -> matches nothing

    def _insert(self, table, rows):
        cols = columns(table)
        sql = (f'INSERT INTO "{self._name(table)}" ({",".join(cols)}) '
               f'VALUES ({",".join("?" * len(cols))})')
        self.con.executemany(sql, [[r.get(c) for c in cols] for r in rows])

    def replace(self, table, rows, where):
        with self.con:                                       # one transaction
            for cond, params in self._conds(where):
                self.con.execute(f'DELETE FROM "{self._name(table)}" WHERE {cond}', params)
            self._insert(table, rows)

    def append(self, table, rows):
        with self.con:
            self._insert(table, rows)

    def read(self, table, columns=None, where=None):
        cols = ", ".join(f'"{c}"' for c in columns) if columns else "*"
        out = []
        for cond, params in self._conds(where):
            out += [dict(r) for r in self.con.execute(
                f'SELECT {cols} FROM "{self._name(table)}" WHERE {cond}', params)]
        return out


# ---------------------------------------------------------------------------- BigQuery
VIEWS = {
    "teams": """
        SELECT team_id, ANY_VALUE(team HAVING MAX season) AS name,
               STRING_AGG(DISTINCT league, ', ') AS leagues,
               MIN(season) AS first_season, MAX(season) AS last_season
        FROM `{core}.team_matches` GROUP BY team_id""",
    "players": """
        SELECT player_id, ANY_VALUE(player HAVING MAX date) AS name,
               ANY_VALUE(team HAVING MAX date) AS latest_team,
               MIN(season) AS first_season, MAX(season) AS last_season,
               COUNT(*) AS games, SUM(goals) AS goals
        FROM `{core}.player_matches` GROUP BY player_id""",
    "player_seasons": """
        SELECT player_id, ANY_VALUE(player HAVING MAX date) AS player, league, season,
               ANY_VALUE(season_label) AS season_label, STRING_AGG(DISTINCT team, ', ') AS teams,
               COUNT(*) AS games, SUM(minutes) AS minutes, SUM(goals) AS goals,
               SUM(goals) - SUM(penalty_goals) AS non_penalty_goals,
               SUM(penalty_goals) AS penalty_goals, SUM(assists) AS assists,
               ROUND(SUM(xg), 2) AS xg, ROUND(SUM(npxg), 2) AS npxg, ROUND(SUM(xa), 2) AS xa,
               SUM(shots) AS shots, SUM(key_passes) AS key_passes,
               SUM(yellow) AS yellow, SUM(red) AS red
        FROM `{core}.player_matches` GROUP BY player_id, league, season""",
    "latest_checks": """
        SELECT * FROM `{core}.quality_checks`
        WHERE run_at = (SELECT MAX(run_at) FROM `{core}.quality_checks`)""",
}


class BigQueryWarehouse(Warehouse):
    """Needs `pip install google-cloud-bigquery` and credentials (gcloud auth
    application-default login, or a service account in GOOGLE_APPLICATION_CREDENTIALS).
    DELETE/INSERT need a project with billing enabled (the free tier covers this volume);
    the BigQuery sandbox does not allow them and expires tables after 60 days."""

    def __init__(self, project=None, location=None, datasets=None):
        from google.cloud import bigquery                    # imported here: optional dependency
        self.bq = bigquery
        self.project = project or config.PROJECT
        self.location = location or config.LOCATION
        self.datasets = datasets or config.DATASETS
        self.client = bigquery.Client(project=self.project, location=self.location)
        self.queries, self.bytes_billed = 0, 0               # of this session, for usage()

    def _run(self, sql, params=None):
        """Run a query and count what BigQuery bills for it (load jobs are free, not counted)."""
        job = self.client.query(sql, job_config=self.bq.QueryJobConfig(query_parameters=params or []))
        rows = job.result()
        self.queries += 1
        self.bytes_billed += getattr(job, "total_bytes_billed", None) or 0
        return rows

    def usage(self):
        return (f"BigQuery: {self.queries} queries, {self.bytes_billed / 2**30:.2f} GiB billed "
                f"(the project's daily quota is about 20 GiB; 1 TiB a month is free)")

    def _tid(self, table):
        ds, name = table.split(".")
        return f"{self.project}.{self.datasets[ds]}.{name}"

    def _schema(self, table):
        return [self.bq.SchemaField(c, t, mode="NULLABLE") for c, t in TABLES[table]]

    def _create(self, table):
        t = self.bq.Table(self._tid(table), schema=self._schema(table))
        t.description = DESCRIPTIONS.get(table)
        if table in CLUSTERING:
            t.clustering_fields = CLUSTERING[table]
        self.client.create_table(t)

    def ensure(self):
        from google.api_core.exceptions import NotFound
        for ds in self.datasets.values():
            dataset = self.bq.Dataset(f"{self.project}.{ds}")
            dataset.location = self.location
            self.client.create_dataset(dataset, exists_ok=True)
        for table in TABLES:
            tid = self._tid(table)
            try:
                existing = self.client.get_table(tid)
            except NotFound:
                self._create(table)
                continue
            have = {f.name for f in existing.schema}
            if have - set(columns(table)) and not existing.num_rows:
                # an empty table left by an older version of the pipeline, with columns that
                # no longer exist: start it again with the current schema
                self.client.delete_table(tid)
                self._create(table)
                continue
            new = [f for f in self._schema(table) if f.name not in have]
            if new:
                existing.schema = list(existing.schema) + new
                self.client.update_table(existing, ["schema"])
        core = f"{self.project}.{self.datasets['core']}"
        for name, sql in VIEWS.items():
            try:
                old = self.client.get_table(f"{core}.{name}")
                if getattr(old, "table_type", "VIEW") == "TABLE" and not old.num_rows:
                    self.client.delete_table(f"{core}.{name}")   # empty table of an older version
            except NotFound:
                pass
            try:
                self._run(f"CREATE OR REPLACE VIEW `{core}.{name}` AS {sql.format(core=core)}")
            except Exception as e:                           # a view is a convenience, never block a run
                print(f"  warning: view {name} not created ({e})")

    def _param_value(self, typ, v):
        if typ == "DATE" and isinstance(v, str):
            return date.fromisoformat(v)
        if typ == "DATETIME" and isinstance(v, str):
            return datetime.fromisoformat(v)
        return v

    def _cond(self, table, where):
        parts, params = [], []
        for i, (col, op, v) in enumerate(_split_where(where)):
            typ = col_type(table, col)
            if op == "in":
                parts.append(f"`{col}` IN UNNEST(@p{i})")
                params.append(self.bq.ArrayQueryParameter(
                    f"p{i}", typ, [self._param_value(typ, x) for x in v]))
            else:
                parts.append(f"`{col}` {op} @p{i}")
                params.append(self.bq.ScalarQueryParameter(f"p{i}", typ, self._param_value(typ, v)))
        return " AND ".join(parts) or "TRUE", params

    def _load(self, tid, table, rows, disposition):
        cols = columns(table)
        cfg = self.bq.LoadJobConfig(schema=self._schema(table), write_disposition=disposition)
        self.client.load_table_from_json([{c: r.get(c) for c in cols} for r in rows], tid,
                                         job_config=cfg).result()

    def replace(self, table, rows, where):
        tid, cols = self._tid(table), ", ".join(f"`{c}`" for c in columns(table))
        cond, params = self._cond(table, where)
        staging = f"{tid}__staging"
        statements = [f"DELETE FROM `{tid}` WHERE {cond};"]
        if rows:
            self._load(staging, table, rows, "WRITE_TRUNCATE")
            statements.append(f"INSERT INTO `{tid}` ({cols}) SELECT {cols} FROM `{staging}`;")
        sql = "BEGIN TRANSACTION;\n" + "\n".join(statements) + "\nCOMMIT TRANSACTION;"
        try:
            self._run(sql, params)
        finally:
            if rows:
                self.client.delete_table(staging, not_found_ok=True)

    def append(self, table, rows):
        if rows:
            self._load(self._tid(table), table, rows, "WRITE_APPEND")

    @staticmethod
    def _plain(v):
        """Dates come back as objects; the rest of the code works with ISO strings."""
        if isinstance(v, datetime):
            return v.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(v, date):
            return v.isoformat()
        return v

    def read(self, table, columns=None, where=None):
        cols = ", ".join(f"`{c}`" for c in columns) if columns else "*"
        cond, params = self._cond(table, where)
        rows = self._run(f"SELECT {cols} FROM `{self._tid(table)}` WHERE {cond}", params)
        return [{k: self._plain(v) for k, v in row.items()} for row in rows]


def open_warehouse(backend, project=None, sqlite_path=None):
    if backend == "sqlite":
        return SQLiteWarehouse(sqlite_path or "football_local.db")
    if backend == "bigquery":
        try:
            return BigQueryWarehouse(project)
        except ImportError:
            raise SystemExit("BigQuery client not installed. Run: pip install -r requirements.txt")
    raise SystemExit(f"Unknown backend {backend!r} (use bigquery or sqlite)")

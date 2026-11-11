#!/usr/bin/env python3
"""
scripts/5_build_index.py - keep the cross-source index (one stable key per real player, team, match).

  python3 scripts/5_build_index.py

The index is the set of CSV files in index/ - they are the master copy and are only ever
ADDED to, never rebuilt, so a key never changes once given:
  index/players.csv   player_key, source, source_id, method, added_at
  index/teams.csv     team_key,   source, source_id, method, added_at
  index/matches.csv   match_key,  source, source_id, method, added_at
  index/merges.csv    kind, old_key, new_key, source, source_id, date   (audit trail)

Each run:
  1. every Transfermarkt id not yet indexed gets a new key
  2. the links found by scripts/3_link.py (incl. config/link_overrides.csv) attach Understat ids to those keys
  3. every Understat id still not indexed gets a new key (= exists only in Understat for now);
     new players and teams among them are listed in index/review.csv
  4. an Understat-only key that scripts/3_link.py now links to a Transfermarkt id is merged into the
     Transfermarkt key (logged in merges.csv)
  5. a link that contradicts the index (the Understat id already sits with ANOTHER
     Transfermarkt id) changes nothing and is listed in index/review.csv
Then football_index.db is written from the CSVs (with names) for scripts/6_to_bigquery.py.

BACK UP THE index/ FOLDER: it is the only part of the project that cannot be recreated
with the same keys.

Run after: scripts/3_link.py and scripts/4_build_sources.py.
No third-party packages needed (Python 3.8+).
"""

import csv
import os
import sqlite3
import sys
from datetime import date

import paths
RAW_DB = paths.WORK_DB
TM_DB = paths.TM_DB
US_DB = paths.US_DB
INDEX_DB = paths.INDEX_DB
INDEX_DIR = paths.INDEX_DIR
TODAY = date.today().isoformat()

TM, US = "transfermarkt", "understat"
KINDS = {   # kind -> (key column, link table in football.db, link columns)
    "players": ("player_key", "link_players", "us_player_id", "tm_player_id"),
    "teams": ("team_key", "link_teams", "us_team_id", "tm_team_id"),
    "matches": ("match_key", "link_matches", "us_match_id", "tm_fixture_id"),
}
MERGE_COLS = ["kind", "old_key", "new_key", "source", "source_id", "date"]
REVIEW_COLS = ["kind", "issue", "key", "source", "source_id", "name", "linked_to_source_id",
               "linked_to_key", "detail"]


# ------------------------------------------------------------------ CSV store
def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, cols, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)          # never leaves a half-written master file


class Index:
    def __init__(self, kind):
        self.kind = kind
        self.key_col = KINDS[kind][0]
        self.path = os.path.join(INDEX_DIR, f"{kind}.csv")
        self.rows = []                       # dicts: key, source, source_id, method, added_at
        self.where = {}                      # (source, source_id) -> key
        for r in read_csv(self.path):
            k, sid = int(r[self.key_col]), int(r["source_id"])
            self.rows.append({"key": k, "source": r["source"], "source_id": sid,
                              "method": r["method"], "added_at": r["added_at"]})
            self.where[(r["source"], sid)] = k
        self.next_key = max((r["key"] for r in self.rows), default=0) + 1
        self.added = []                      # rows added this run
        self.merges = []

    def new_key(self):
        k = self.next_key
        self.next_key += 1
        return k

    def add(self, source, source_id, key, method):
        row = {"key": key, "source": source, "source_id": source_id, "method": method,
               "added_at": TODAY}
        self.rows.append(row)
        self.where[(source, source_id)] = key
        self.added.append(row)

    def sources_of(self, key):
        return {r["source"] for r in self.rows if r["key"] == key}

    def merge(self, old_key, new_key, method):
        """Move every row of old_key to new_key (old_key stops existing)."""
        for r in self.rows:
            if r["key"] == old_key:
                r["key"] = new_key
                r["method"] = method
                self.where[(r["source"], r["source_id"])] = new_key
                self.merges.append({"kind": self.kind, "old_key": old_key, "new_key": new_key,
                                    "source": r["source"], "source_id": r["source_id"],
                                    "date": TODAY})

    def save(self):
        self.rows.sort(key=lambda r: (r["key"], r["source"], r["source_id"]))
        write_csv(self.path, [self.key_col, "source", "source_id", "method", "added_at"],
                  [{self.key_col: r["key"], "source": r["source"], "source_id": r["source_id"],
                    "method": r["method"], "added_at": r["added_at"]} for r in self.rows])


# ------------------------------------------------------------------ inputs
def ids_and_names(db, sql):
    con = sqlite3.connect(db)
    out = {sid: name for sid, name in con.execute(sql) if sid is not None}
    con.close()
    return out


def load_inputs():
    for path, hint in ((RAW_DB, "scripts/1_transfermarkt.py build, scripts/2_understat.py build, scripts/3_link.py"),
                       (TM_DB, "scripts/4_build_sources.py"), (US_DB, "scripts/4_build_sources.py")):
        if not os.path.exists(path):
            sys.exit(f"{os.path.basename(path)} not found. Run {hint} first.")
    names = {
        ("players", TM): ids_and_names(TM_DB, "SELECT player_id, name FROM players"),
        ("players", US): ids_and_names(US_DB, "SELECT player_id, name FROM players"),
        ("teams", TM): ids_and_names(TM_DB, "SELECT team_id, name FROM teams"),
        ("teams", US): ids_and_names(US_DB, "SELECT team_id, name FROM teams"),
        ("matches", TM): ids_and_names(TM_DB, "SELECT match_id, date || ' ' || COALESCE(home_team, '?') "
                                              "|| ' v ' || COALESCE(away_team, '?') FROM matches"),
        ("matches", US): ids_and_names(US_DB, "SELECT match_id, date || ' ' || COALESCE(home_team, '?') "
                                              "|| ' v ' || COALESCE(away_team, '?') FROM matches "
                                              "UNION ALL SELECT match_id, date || ' ' || "
                                              "COALESCE(home_team, '?') || ' v ' || "
                                              "COALESCE(away_team, '?') FROM fixtures"),
    }
    con = sqlite3.connect(RAW_DB)
    links = {}
    for kind, (_, table, us_col, tm_col) in KINDS.items():
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            sys.exit(f"football.db has no {table}. Run scripts/3_link.py first.")
        links[kind] = sorted(con.execute(
            f"SELECT {us_col}, {tm_col}, method FROM {table} "
            f"WHERE {us_col} IS NOT NULL AND {tm_col} IS NOT NULL"))
    con.close()
    return names, links


# ------------------------------------------------------------------ the update
def update(idx, tm_names, us_names, links, review):
    kind = idx.kind
    # 1. new Transfermarkt ids (plus linked ids missing from the tables, e.g. old profiles)
    tm_ids = set(tm_names) | {tm for _, tm, _ in links}
    for tm in sorted(tm_ids):
        if (TM, tm) not in idx.where:
            idx.add(TM, tm, idx.new_key(), "new")
    # 2. + 4. + 5. links
    for us, tm, method in links:
        kt = idx.where[(TM, tm)]
        ku = idx.where.get((US, us))
        if ku is None:
            idx.add(US, us, kt, f"link:{method}")
        elif ku != kt:
            if TM not in idx.sources_of(ku):           # Understat-only key: now linked -> merge
                idx.merge(ku, kt, f"link:{method}")
            else:                                     # contradicts what the index holds
                review.append({"kind": kind, "issue": "link contradicts index", "key": ku,
                               "source": US, "source_id": us, "name": us_names.get(us),
                               "linked_to_source_id": tm, "linked_to_key": kt,
                               "detail": f"scripts/3_link.py ({method}) now says Transfermarkt {tm} "
                                         f"({tm_names.get(tm)}); index unchanged"})
    # 3. Understat ids with no link: their own key
    for us in sorted(us_names):
        if (US, us) not in idx.where:
            k = idx.new_key()
            idx.add(US, us, k, "new")
            if kind == "matches":       # current-season matches are Understat-only by design
                continue
            review.append({"kind": kind, "issue": "new, Understat only", "key": k,
                           "source": US, "source_id": us, "name": us_names.get(us),
                           "linked_to_source_id": None, "linked_to_key": None,
                           "detail": "no Transfermarkt match; check for a missed link"})


def write_db(indexes, names, all_merges):
    if os.path.exists(INDEX_DB):
        os.remove(INDEX_DB)
    con = sqlite3.connect(INDEX_DB)
    for kind, idx in indexes.items():
        k = idx.key_col
        con.execute(f"CREATE TABLE {kind} ({k} INTEGER, source TEXT, source_id INTEGER, "
                    "name TEXT, method TEXT, added_at TEXT)")
        con.executemany(f"INSERT INTO {kind} VALUES (?,?,?,?,?,?)", [
            (r["key"], r["source"], r["source_id"], names[(kind, r["source"])].get(r["source_id"]),
             r["method"], r["added_at"]) for r in idx.rows])
    con.execute("CREATE TABLE merges (kind TEXT, old_key INTEGER, new_key INTEGER, source TEXT, "
                "source_id INTEGER, date TEXT)")
    con.executemany("INSERT INTO merges VALUES (?,?,?,?,?,?)",
                    [tuple(m[c] for c in MERGE_COLS) for m in all_merges])
    con.commit()
    con.close()


def main():
    names, links = load_inputs()
    os.makedirs(INDEX_DIR, exist_ok=True)
    first_run = not os.path.exists(os.path.join(INDEX_DIR, "players.csv"))
    merges_path = os.path.join(INDEX_DIR, "merges.csv")
    old_merges = read_csv(merges_path)
    review, indexes, new_merges = [], {}, []
    print("Updating the index" + (" (first run: every id gets a key)" if first_run else "") + "...")
    print(f"  {'':<9}{'keys':>8}{'tm ids':>9}{'us ids':>9}{'both':>8}{'added':>8}{'merged':>8}")
    for kind in KINDS:
        idx = Index(kind)
        update(idx, names[(kind, TM)], names[(kind, US)], links[kind], review)
        idx.save()
        indexes[kind] = idx
        new_merges += idx.merges
        keys = {r["key"] for r in idx.rows}
        by_key = {}
        for r in idx.rows:
            by_key.setdefault(r["key"], set()).add(r["source"])
        both = sum(1 for s in by_key.values() if len(s) > 1)
        n_tm = sum(1 for r in idx.rows if r["source"] == TM)
        n_us = sum(1 for r in idx.rows if r["source"] == US)
        print(f"  {kind:<9}{len(keys):>8,}{n_tm:>9,}{n_us:>9,}{both:>8,}{len(idx.added):>8,}"
              f"{len({m['old_key'] for m in idx.merges}):>8,}")
    write_csv(merges_path, MERGE_COLS, old_merges + new_merges)
    review_path = os.path.join(INDEX_DIR, "review.csv")
    write_csv(review_path, REVIEW_COLS, review)

    # sanity checks: a source id sits under one key only; a key has at most one id per source
    problems = 0
    for kind, idx in indexes.items():
        seen, per_key = set(), {}
        for r in idx.rows:
            sid = (r["source"], r["source_id"])
            problems += sid in seen
            seen.add(sid)
            per_key.setdefault((r["key"], r["source"]), []).append(r["source_id"])
        multi = {k: v for k, v in per_key.items() if len(v) > 1}
        if multi:
            print(f"  note: {len(multi)} {kind} keys hold more than one id from the same source "
                  "(e.g. a club or player listed twice by that source)")
    if problems:
        print(f"  WARNING: {problems} source ids appear twice in the index - please report this")
    write_db(indexes, names, [dict(m) for m in old_merges] + new_merges)

    contra = sum(1 for r in review if r["issue"] == "link contradicts index")
    new_us = sum(1 for r in review if r["issue"] == "new, Understat only")
    print(f"\n  review.csv: {new_us} new Understat-only ids, {contra} links that contradict the index")
    if first_run:
        print("  (first run: every Understat-only id is listed once; later runs list only new ones)")
    print("  Fix a missed link in config/link_overrides.csv, then run scripts/3_link.py and scripts/5_build_index.py again:"
          " the Understat-only key is merged into the Transfermarkt one.")
    print(f"\nDone. football_index.db written. BACK UP the index/ folder.")


if __name__ == "__main__":
    main()

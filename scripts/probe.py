"""TEMPORARY real-data trial. Reads BigQuery (read-only), copies raw.* and the draft history
into a SQLite file, then runs rebuild + checks + tweets there. Removed before merge."""
import json
import sys
from collections import Counter, defaultdict
from datetime import date

from google.cloud import bigquery

sys.path.insert(0, ".")
from football import checks, pipeline, tweets          # noqa: E402
from football.warehouse import SQLiteWarehouse        # noqa: E402

P = "football-model-494509"
c = bigquery.Client(project=P, location="EU")
wh = SQLiteWarehouse("trial.db")
wh.ensure()


def copy(table, bq, where=""):
    rows = [dict(r) for r in c.query(f"SELECT * FROM `{P}.{bq}` {where}").result()]
    rows = [{k: wh_val(v) for k, v in r.items()} for r in rows]
    wh.append(table, rows)
    return len(rows)


def wh_val(v):
    from datetime import datetime
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.isoformat()
    return v


print("raw.league", copy("raw.league", "football_raw.league"))
for y in range(2020, 2027):
    print("raw.match", y, copy("raw.match", "football_raw.match", f"WHERE season = {y}"), flush=True)
print("drafts", copy("tweets.drafts", "football_tweets.drafts"))
print("runs", copy("core.runs", "football_core.runs"))

print("\n===== rebuild")
pipeline.rebuild(wh)
today = date.today()
print("\n===== checks")
checks.run_checks(wh, today, pipeline.utcnow())
print("\n===== coverage")
for r in checks.coverage(wh, today):
    print(f"  {r['league']:<16}{r['season']} teams {r['teams']} expected {r['expected']} listed {r['listed']} "
          f"played {r['played']} details {r['details']} unusable {len(r['unusable'])} no_xg {r['no_xg']}")

print("\n===== null share of new columns (current season)")
pm = wh.read("core.player_matches", None, {"season": 2026})
for col in ("tackles", "dribbles", "big_chances_created", "xa", "key_passes", "saves", "goals_prevented",
            "xgot_faced", "touches_in_box", "interceptions"):
    print(f"  {col}: {sum(p[col] is None for p in pm)}/{len(pm)} null")
ts = wh.read("core.team_stats", None, {"season": 2026})
for col in ("big_chances_missed", "xg_open_play", "xg_set_play", "keeper_saves", "xgot"):
    print(f"  team {col}: {sum(t[col] is None for t in ts)}/{len(ts)} null")

print("\n===== all facts by kind (top 6 each)")
d = tweets.Data(wh, None, today)
facts = tweets.find_facts(d)
print(Counter(f.kind for f in facts))
by = defaultdict(list)
for f in facts:
    by[f.kind].append(f)
for k, fs in by.items():
    print(f"\n--- {k} ({len(fs)})")
    for f in sorted(fs, key=lambda f: -f.score)[:6]:
        print(f"[{f.score}] {f.final}\n   evidence: {json.dumps(f.evidence, ensure_ascii=False)}\n")

print("\n===== drafts as the daily run would choose them (repeats allowed, to compare)")
for i, f in enumerate(tweets.draft(wh, today, top=25, include_repeats=True, log=print), 1):
    print(f"\n{i}. [{f.kind} {f.score}] {f.final}")

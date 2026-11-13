"""TEMPORARY read-only probe of the real BigQuery data. Removed before merge."""
import json
from collections import Counter, defaultdict
from google.cloud import bigquery

P = "football-model-494509"
c = bigquery.Client(project=P, location="EU")


def q(sql):
    print("\n### " + " ".join(sql.split())[:140])
    rows = list(c.query(sql).result())
    for r in rows:
        print("  " + " | ".join(str(v) for v in r.values()))
    return rows


q(f"""SELECT m.league, m.season, COUNT(DISTINCT m.match_id) played,
  COUNT(DISTINCT IF(t.team_id IS NOT NULL, m.match_id, NULL)) detailed,
  COUNT(DISTINCT IF(t.xg IS NOT NULL, m.match_id, NULL)) with_xg,
  COUNT(DISTINCT IF(s.match_id IS NOT NULL, m.match_id, NULL)) with_shots,
  COUNT(DISTINCT m.home_team_id) teams
FROM `{P}.football_core.matches` m
LEFT JOIN `{P}.football_core.team_stats` t USING (match_id)
LEFT JOIN (SELECT DISTINCT match_id FROM `{P}.football_core.shots`) s USING (match_id)
GROUP BY 1,2 ORDER BY 1,2""")
q(f"""SELECT league, season, status, COUNT(*) FROM `{P}.football_core.fixtures`
 WHERE status != 'upcoming' GROUP BY 1,2,3 ORDER BY 1,2""")
q(f"""SELECT r.match_id, r.league, r.season, r.problem, m.home_team, m.away_team, m.date
 FROM `{P}.football_raw.match` r LEFT JOIN `{P}.football_core.matches` m USING (match_id)
 WHERE r.problem IS NOT NULL""")
q(f"""SELECT match_id, league, season, date, kickoff_utc, home_team, away_team, status
 FROM `{P}.football_core.fixtures` WHERE status='cancelled'""")
q(f"""SELECT league, season, COUNTIF(xg IS NULL) xg_null, COUNTIF(xa IS NULL) xa_null,
 COUNTIF(rating IS NULL) rating_null, COUNTIF(minutes IS NULL) min_null, COUNT(*) n
 FROM `{P}.football_core.player_matches` GROUP BY 1,2 ORDER BY 1,2""")
q(f"""SELECT league, season, COUNTIF(xgot IS NULL) xgot_null, COUNTIF(big_chances IS NULL) bc_null,
 COUNTIF(possession IS NULL) poss_null, COUNT(*) n FROM `{P}.football_core.team_stats`
 GROUP BY 1,2 ORDER BY 1,2""")
q(f"""SELECT situation, shot_type, COUNT(*) FROM `{P}.football_core.shots` WHERE season=2026
 GROUP BY 1,2 ORDER BY 3 DESC""")

# every player-stat key and team-stat key in the slim copies of this season
keys, tkeys, ex = Counter(), Counter(), {}
for r in c.query(f"SELECT payload FROM `{P}.football_raw.match` WHERE season=2026 AND problem IS NULL LIMIT 60").result():
    s = json.loads(r["payload"])
    for k in s["team_stats"]:
        tkeys[k] += 1
    for p in s["players"]:
        for k, v in p["stats"].items():
            keys[k] += 1
            ex.setdefault(k, v)
print("\n### player stat keys (count in 60 matches, example)")
for k, n in keys.most_common():
    print(f"  {k}: {n}  e.g. {ex[k]}")
print("\n### team stat keys")
for k, n in tkeys.most_common():
    print(f"  {k}: {n}")

q(f"""SELECT * FROM `{P}.football_tweets.drafts` WHERE run_date = (SELECT MAX(run_date) FROM `{P}.football_tweets.drafts`) ORDER BY rank""")
q(f"""SELECT type, COUNT(*) FROM `{P}.football_tweets.drafts` GROUP BY 1""")

"""TEMPORARY probe (remove before any merge): the 14 old matches whose players' goals do not
add up to the score. READ ONLY: SELECT queries on BigQuery, no writes."""

import json

from football import config
from football.warehouse import BigQueryWarehouse

wh = BigQueryWarehouse()
P, D = wh.project, wh.datasets
core, raw = f"`{P}.{D['core']}`", f"`{P}.{D['raw']}`"
OLD = config.PLAYER_STATS_FROM

bad = list(wh._run(f"""
  WITH p AS (SELECT match_id, home_away, SUM(goals) g, SUM(own_goals) og, COUNT(*) n,
                    COUNT(DISTINCT player_id) np
             FROM {core}.player_matches WHERE season < {OLD} AND goals IS NOT NULL
             GROUP BY match_id, home_away),
  s AS (SELECT match_id,
          SUM(IF(home_away='home', g, 0)) + SUM(IF(home_away='away', og, 0)) AS home_calc,
          SUM(IF(home_away='away', g, 0)) + SUM(IF(home_away='home', og, 0)) AS away_calc,
          SUM(n) rows_, SUM(np) players_
        FROM p GROUP BY match_id)
  SELECT m.*, s.home_calc, s.away_calc, s.rows_, s.players_
  FROM s JOIN {core}.matches m USING (match_id)
  WHERE m.season < {OLD} AND (s.home_calc != m.home_goals OR s.away_calc != m.away_goals)
  ORDER BY m.season"""))
print(f"== {len(bad)} matches")
ids = ",".join(str(b.match_id) for b in bad) or "0"
pl = {}
for r in wh._run(f"""SELECT match_id, player_id, player, team, home_away, goals, own_goals
                     FROM {core}.player_matches WHERE season < {OLD} AND match_id IN ({ids})
                     AND (goals > 0 OR own_goals > 0)"""):
    pl.setdefault(r.match_id, []).append(f"{r.player} ({r.home_away}, id {r.player_id}) g{r.goals} og{r.own_goals}")
pay = {x.match_id: json.loads(x.payload) for x in
       wh._run(f"SELECT match_id, payload FROM {raw}.match WHERE season < {OLD} AND match_id IN ({ids})")}
for b in bad:
    print(f"-- {b.season} {b.league}: {b.home_team} {b.home_goals}-{b.away_goals} {b.away_team} "
          f"(match {b.match_id}); players add up to {b.home_calc}-{b.away_calc}; rows {b.rows_}, distinct players {b.players_}")
    print("   player rows with goals: " + "; ".join(pl.get(b.match_id, [])))
    s = pay.get(b.match_id) or {}
    print("   goal events: " + "; ".join(f"{e.get('player')} home={e.get('is_home')} og={e.get('own_goal')} min={e.get('minute')}"
                                         for e in s.get("goals") or []))
    for side in ("home", "away"):
        names = dict((str(i), n) for i, n in (s.get("lineup") or {}).get(side, {}).get("names") or [])
        ids_ = [str(i) for i in (s.get("lineup") or {}).get(side, {}).get("starters") or []]
        dup = [n for n in set(names.values()) if list(names.values()).count(n) > 1]
        print(f"   {side}: {len(names)} named, ids {len(set(names))}, duplicate names {dup}, id 0 present: {'0' in names}")
print(wh.usage())

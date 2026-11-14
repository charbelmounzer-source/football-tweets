"""TEMPORARY probe (remove before any merge): how well old goal events (2010/11-2015/16)
were matched to the lineups by name. READ ONLY: SELECT queries on BigQuery, no writes."""

import collections
import json

from football import config, model
from football.warehouse import BigQueryWarehouse

wh = BigQueryWarehouse()
P, D = wh.project, wh.datasets
core, raw = f"`{P}.{D['core']}`", f"`{P}.{D['raw']}`"
OLD = config.PLAYER_STATS_FROM          # 2016: before this, goals come from the events

print("== per league-season, matches with details (2010/11-2015/16)")
rows = wh._run(f"""
  SELECT league, season,
         COUNT(DISTINCT match_id) AS detailed,
         COUNT(DISTINCT IF(goals IS NULL, match_id, NULL)) AS goals_null,
         COUNT(DISTINCT IF(yellow IS NULL, match_id, NULL)) AS cards_null
  FROM {core}.player_matches WHERE season < {OLD}
  GROUP BY league, season ORDER BY season, league""")
tot = collections.Counter()
print(f"  {'league':15} season  detailed  goals NULL   cards NULL")
for r in rows:
    tot.update(detailed=r.detailed, goals=r.goals_null, cards=r.cards_null)
    print(f"  {r.league:15} {r.season}/{(r.season + 1) % 100:02d}  {r.detailed:8}  "
          f"{r.goals_null:5} ({100 * r.goals_null / r.detailed:4.1f}%)  "
          f"{r.cards_null:5} ({100 * r.cards_null / r.detailed:4.1f}%)")
print(f"  TOTAL {tot['detailed']} matches: goals NULL {tot['goals']} "
      f"({100 * tot['goals'] / max(1, tot['detailed']):.1f}%), cards NULL {tot['cards']}")

print("== sanity: where goals are known, player goals add up to the score")
r = list(wh._run(f"""
  WITH p AS (SELECT match_id, home_away, SUM(goals) g, SUM(own_goals) og
             FROM {core}.player_matches WHERE season < {OLD} AND goals IS NOT NULL
             GROUP BY match_id, home_away),
  s AS (SELECT p.match_id,
          SUM(IF(home_away='home', g, 0)) + SUM(IF(home_away='away', og, 0)) AS home_calc,
          SUM(IF(home_away='away', g, 0)) + SUM(IF(home_away='home', og, 0)) AS away_calc
        FROM p GROUP BY p.match_id)
  SELECT COUNT(*) n, COUNTIF(s.home_calc != m.home_goals OR s.away_calc != m.away_goals) bad
  FROM s JOIN {core}.matches m USING (match_id) WHERE m.season < {OLD}"""))[0]
print(f"  {r.n} matches with known goals, {r.bad} where the players' goals differ from the score")

print("== why goals are NULL: re-run the name match on the stored answers")
null_ids = [x.match_id for x in wh._run(f"""
  SELECT DISTINCT match_id FROM {core}.player_matches WHERE season < {OLD} AND goals IS NULL""")]
reasons, missed, examples = collections.Counter(), collections.Counter(), []
if null_ids:
    ids = ",".join(str(i) for i in null_ids)
    m = {x.match_id: x for x in wh._run(f"SELECT * FROM {core}.matches WHERE season < {OLD} AND match_id IN ({ids})")}
    for x in wh._run(f"SELECT match_id, payload FROM {raw}.match WHERE season < {OLD} AND match_id IN ({ids})"):
        slim, mt = json.loads(x.payload), m.get(x.match_id)
        if mt is None:
            reasons["not in core.matches"] += 1
            continue
        came_on = {pid for sw in slim.get("swaps") or [] for pid in sw if pid is not None}
        find, people = {}, {}
        for side in ("home", "away"):
            lu = slim["lineup"][side]
            named = {model.num(pid, int): n for pid, n in lu.get("names") or []}
            played = {model.num(p, int) for p in lu["starters"]} | ({model.num(p, int) for p in lu["subs"]} & came_on)
            people[side == "home"] = [named.get(pid) for pid in played]
            find[side == "home"] = model.name_finder([(pid, named.get(pid)) for pid in played])
        by_side, why = {True: 0, False: 0}, None
        for e in slim.get("goals") or []:
            side = e.get("is_home")
            if side not in (True, False):
                why = "goal event without a side"
                break
            pid = find[side](e.get("player")) if not e["own_goal"] else (
                find[not side](e.get("player")) or find[side](e.get("player")))
            if pid is None:
                why = "scorer not found in the lineup" + (" (own goal)" if e["own_goal"] else "")
                missed[e.get("player")] += 1
                if len(examples) < 25:
                    cand = [n for n in people[side] if n and model.plain(e.get("player") or "").split(" ")[-1][:4]
                            in model.plain(n)]
                    examples.append(f"{mt.season} {mt.league}: {mt.home_team}-{mt.away_team}: event name "
                                    f"{e.get('player')!r}, own goal {e['own_goal']}; similar in lineup: {cand}")
                break
            by_side[side] += 1
        if why is None:
            got = (by_side[True], by_side[False])
            own = sum(1 for e in slim.get("goals") or [] if e["own_goal"])
            why = (f"events {got[0]}-{got[1]} vs score {mt.home_goals}-{mt.away_goals}"
                   f"{' (has own goal)' if own else ''}") if got != (mt.home_goals, mt.away_goals) else "?? matches now"
            if got != (mt.home_goals, mt.away_goals):
                why = "event total differs from score" + (" (has own goal)" if own else "")
                if len(examples) < 40:
                    examples.append(f"{mt.season} {mt.league}: {mt.home_team}-{mt.away_team} "
                                    f"{mt.home_goals}-{mt.away_goals}: events {got[0]}-{got[1]}, own goals {own}")
        reasons[why] += 1
for k, v in reasons.most_common():
    print(f"  {v:5}  {k}")
print("== most frequent unmatched names")
for k, v in missed.most_common(20):
    print(f"  {v:3}  {k!r}")
print("== examples")
for e in examples:
    print("  " + e)
print(wh.usage())

"""TEMPORARY trial of the new model on real older FotMob match pages (no BigQuery):
for a sample of matches per league and season 2010/11-2019/20, does validate_match accept
them, and how many old matches get their scorers and cards matched to the lineup by name?"""
import sys
import urllib.parse
from collections import Counter

sys.path.insert(0, ".")
from football import config                                     # noqa: E402
from football.fotmob import Client, FotMobError                   # noqa: E402
from football.model import model_league, model_match, slim_league, slim_match, validate_match  # noqa: E402
from football.pipeline import utcnow                             # noqa: E402

c = Client(max_requests=400)
now = utcnow()
total = Counter()
for code in config.LEAGUES:
    for season in (2010, 2012, 2014, 2015, 2017, 2019):
        q = urllib.parse.quote(config.fotmob_season(season), safe="")
        league = model_league(config.LEAGUES[code], season,
                              slim_league(c.get_json(f"leagues?id={config.FOTMOB_IDS[code]}&season={q}")), now)
        ms = sorted(league["matches"], key=lambda m: m["kickoff_utc"])
        sample = ms[:: max(len(ms) // 5, 1)][:5]
        stats = Counter()
        for m in sample:
            try:
                slim = slim_match(c.get_json(f"matchDetails?matchId={m['match_id']}"))
            except FotMobError as e:
                stats["error"] += 1
                continue
            problem = validate_match(slim, m)
            if problem:
                stats[f"unusable: {problem}"] += 1
                continue
            rows = model_match(m, slim)
            pl = rows["player_matches"]
            stats["usable"] += 1
            stats["players_rows"] += len(pl)
            if season < config.PLAYER_STATS_FROM:
                if pl and all(p["goals"] is not None for p in pl):
                    stats["goals_known"] += 1
                else:
                    stats["goals_unknown"] += 1
                    print(f"    goals unknown: {m['home_team']} {m['home_goals']}-{m['away_goals']} {m['away_team']} "
                          f"events={[(g['player'], g['is_home'], g['own_goal']) for g in slim['goals']]}")
                if pl and all(p["yellow"] is not None for p in pl):
                    stats["cards_known"] += 1
                if any(p["assists"] is not None or p["minutes"] is not None for p in pl):
                    stats["BUG_not_null"] += 1
            else:
                if any(p["goals"] is None for p in pl):
                    stats["BUG_goals_null"] += 1
                if any(p["xg"] is not None for p in pl):
                    stats["xg_present"] += 1
        total.update(stats)
        print(f"{config.LEAGUES[code]} {config.season_label(season)}: {dict(stats)}", flush=True)
print(f"\nTOTAL {dict(total)}\nrequests used: {c.used}")

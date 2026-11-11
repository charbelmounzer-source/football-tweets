"""Data-quality gate, run after every update on the current season.

error = the data can't be trusted: the daily job stops before drafting tweets.
warn  = look at it (some are real Understat quirks); tweets are still drafted.
Every result is stored in core.quality_checks (latest run: view core.latest_checks).
"""

from collections import Counter, defaultdict
from datetime import timedelta

from . import config

DT = "%Y-%m-%d %H:%M:%S"


def run_checks(wh, today, now, leagues=None, log=print):
    season = config.current_season(today)
    names = [config.LEAGUES[c] for c in (leagues or list(config.LEAGUES))]
    scope = {"season": season}
    matches = [m for m in wh.read("core.matches", None, scope) if m["league"] in names]
    fixtures = [m for m in wh.read("core.fixtures", None, scope) if m["league"] in names]
    team_rows = [r for r in wh.read("core.team_matches", None, scope) if r["league"] in names]
    players = wh.read("core.player_matches", ["match_id", "player_id", "player", "team_id", "goals",
                                              "own_goals", "started"], scope)
    shots = wh.read("core.shots", ["match_id", "player_id", "result"], scope)
    raw = {r["league"]: r["fetched_at"] for r in wh.read("raw.league", ["league", "fetched_at"], scope)}
    results = []

    def check(cid, level, description, bad):
        bad = list(bad)
        results.append({"run_at": now.strftime(DT), "check_id": cid, "level": level,
                        "description": description, "row_count": len(bad),
                        "status": level if bad else "ok",
                        "sample": "; ".join(str(b) for b in bad[:10]) or None})

    # freshness. In July-August a league may simply not have started on Understat yet.
    limit = (now - timedelta(hours=36)).strftime(DT)
    preseason = today.month in (7, 8)
    check("F1", "warn" if preseason else "error",
          "League not downloaded in the last 36 hours",
          [f"{lg} (last: {raw.get(lg, 'never')})" for lg in names if raw.get(lg, "") < limit])

    by_id = Counter(m["match_id"] for m in matches + fixtures)
    check("M1", "error", "Match id present twice (or in both matches and fixtures)",
          [mid for mid, n in by_id.items() if n > 1])

    sides = defaultdict(list)
    for r in team_rows:
        sides[r["match_id"]].append(r)
    check("M2", "error", "Played match without exactly 2 team rows",
          [m["match_id"] for m in matches if len(sides.get(m["match_id"], [])) != 2]
          + [f"team {r['team']} {r['date']} (no match)" for r in sides.get(None, [])])

    def score_differs(m):
        rows = {r["team_id"]: r for r in sides.get(m["match_id"], [])}
        h, a = rows.get(m["home_team_id"]), rows.get(m["away_team_id"])
        return bool(h and a) and (h["goals_for"], a["goals_for"]) != (m["home_goals"], m["away_goals"])
    check("M3", "error", "Team rows disagree with the match score",
          [m["match_id"] for m in matches if score_differs(m)])

    detailed = defaultdict(list)
    for p in players:
        detailed[p["match_id"]].append(p)
    old = (today - timedelta(days=2)).isoformat()
    check("D1", "warn", "Match played 2+ days ago without player details",
          [m["match_id"] for m in matches if m["match_id"] not in detailed and (m["date"] or "") <= old])

    def goals_differ(m):
        rows = detailed.get(m["match_id"])
        if not rows:
            return False
        h = sum((p["goals"] or 0) if p["team_id"] == m["home_team_id"] else (p["own_goals"] or 0) for p in rows)
        a = sum((p["goals"] or 0) if p["team_id"] == m["away_team_id"] else (p["own_goals"] or 0) for p in rows)
        return (h, a) != (m["home_goals"], m["away_goals"])
    check("D2", "warn", "Score differs from players' goals + own goals",
          [m["match_id"] for m in matches if goals_differ(m)])

    shot_goals, with_shots = Counter(), set()
    for x in shots:
        with_shots.add(x["match_id"])
        if x["result"] == "Goal":
            shot_goals[(x["match_id"], x["player_id"])] += 1
    check("D3", "warn", "Player's goals differ from his goals in the shot data",
          [f"{p['player']} (match {p['match_id']})" for p in players if p["match_id"] in with_shots
           and (p["goals"] or 0) != shot_goals.get((p["match_id"], p["player_id"]), 0)])

    starters = Counter((p["match_id"], p["team_id"]) for p in players if p["started"])
    check("D4", "warn", "Team without exactly 11 starters",
          [f"match {mid} team {tid}: {n}" for (mid, tid), n in starters.items() if n != 11])

    check("X1", "warn", "Fixtures postponed (kick-off passed 2+ days ago, no result)",
          [f"{f['home_team']} v {f['away_team']} {f['date']}" for f in fixtures
           if f["status"] == "postponed"])

    wh.append("core.quality_checks", results)
    errors = [r for r in results if r["status"] == "error"]
    for r in results:
        if r["status"] != "ok":
            log(f"  {r['status'].upper():<5} {r['check_id']} {r['description']}: {r['row_count']} "
                f"({r['sample']})")
    log(f"  checks: {len(results)} run, {len(errors)} errors, "
        f"{sum(r['status'] == 'warn' for r in results)} warnings")
    return results, len(errors)

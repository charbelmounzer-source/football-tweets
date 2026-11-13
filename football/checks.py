"""Data-quality gate, run after every update on the current season.

error = the data can't be trusted: the daily job stops before drafting tweets.
warn  = look at it (some are real quirks of the source); tweets are still drafted.
Every result is stored in core.quality_checks (latest run: view core.latest_checks).

The H checks look at every season loaded, the others at the current season.
"""

from collections import Counter, defaultdict
from datetime import timedelta

from . import config

DT = "%Y-%m-%d %H:%M:%S"
MIN_COVERAGE = 0.98      # below this share of matches (or of match details), a season has a hole
MAX_XG_MISSING = 0.02    # above this share of detailed matches without xG: warning
SETTLE_DAYS = 2          # a match this recent may not have its details yet


def coverage(wh, today, leagues=None):
    """One row per league-season: how much of it is in the warehouse.
    expected = teams x (teams - 1), a double round robin. FotMob lists every match of the
    season from the start, so `listed` (played + without a result) must reach it."""
    names = set(leagues) if leagues else None
    season_now = config.current_season(today)
    settled = (today - timedelta(days=SETTLE_DAYS)).isoformat()
    played = [m for m in wh.read("core.matches", ["match_id", "league", "season", "date",
                                                  "home_team_id", "away_team_id"])
              if not names or m["league"] in names]
    fixtures = [f for f in wh.read("core.fixtures", ["league", "season", "status", "home_team_id",
                                                     "away_team_id"])
                if not names or f["league"] in names]
    raw = {r["match_id"]: r["problem"] for r in wh.read("raw.match", ["match_id", "problem"])}
    xg = {r["match_id"] for r in wh.read("core.team_stats", ["match_id", "xg"]) if r["xg"] is not None}
    out = {}

    def row(m):
        return out.setdefault((m["league"], m["season"]), {
            "league": m["league"], "season": m["season"], "teams": set(), "listed": 0, "played": 0,
            "cancelled": 0, "settled": 0, "details": 0, "settled_details": 0, "unusable": [], "pending": [], "no_xg": 0})
    for f in fixtures:
        r = row(f)
        r["teams"] |= {f["home_team_id"], f["away_team_id"]}
        r["listed"] += 1
        r["cancelled"] += f["status"] == "cancelled"
    for m in played:
        r = row(m)
        r["teams"] |= {m["home_team_id"], m["away_team_id"]}
        r["listed"] += 1
        r["played"] += 1
        old = (m["date"] or "9") <= settled          # recent matches may still be on their way
        r["settled"] += old
        if m["match_id"] not in raw:
            if old:
                r["pending"].append(m["match_id"])
        elif raw[m["match_id"]]:
            r["unusable"].append(m["match_id"])
        else:
            r["details"] += 1
            r["settled_details"] += old
            r["no_xg"] += m["match_id"] not in xg
    for r in out.values():
        n = len(r["teams"])
        r["teams"], r["expected"] = n, n * (n - 1)
        r["finished"] = r["season"] < season_now
        r["share_played"] = r["played"] / r["expected"] if r["expected"] else 0
        r["share_details"] = r["settled_details"] / r["settled"] if r["settled"] else 1
    return [out[k] for k in sorted(out)]


def run_checks(wh, today, now, leagues=None, log=print):
    season = config.current_season(today)
    names = [config.LEAGUES[c] for c in (leagues or list(config.LEAGUES))]
    scope = {"season": season}

    def read(table, columns=None):
        return [r for r in wh.read(table, columns, scope) if r["league"] in names]
    matches, fixtures, team_rows = read("core.matches"), read("core.fixtures"), read("core.team_matches")
    standings = read("core.standings")
    team_stats = read("core.team_stats", ["match_id", "league", "team_id", "team", "xg", "shot_goals"])
    players = read("core.player_matches", ["match_id", "league", "player_id", "player", "team_id",
                                           "goals", "own_goals", "started"])
    shots = read("core.shots", ["match_id", "league", "player_id", "team_id", "result", "own_goal", "xg"])
    raw = {r["league"]: r["fetched_at"] for r in wh.read("raw.league", ["league", "fetched_at"], scope)}
    unusable = {r["match_id"] for r in wh.read("raw.match", ["match_id", "problem"], scope) if r["problem"]}
    results = []

    def check(cid, level, description, bad):
        bad = list(bad)
        results.append({"run_at": now.strftime(DT), "check_id": cid, "level": level,
                        "description": description, "row_count": len(bad),
                        "status": level if bad else "ok",
                        "sample": "; ".join(str(b) for b in bad[:10]) or None})

    # freshness. In July-August a league may simply not have its new season on FotMob yet.
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
          [m["match_id"] for m in matches if len(sides.get(m["match_id"], [])) != 2])

    # the league table is a second, independent count by FotMob: it must agree with the results
    mine = defaultdict(lambda: [0, 0])
    for r in team_rows:
        mine[(r["league"], r["team_id"])][0] += 1
        mine[(r["league"], r["team_id"])][1] += r["points"]
    check("S1", "warn", "League table disagrees with the results (played, points)",
          [f"{t['team']}: table {t['played']} played {t['points']} pts, results "
           f"{mine[(t['league'], t['team_id'])][0]} played {mine[(t['league'], t['team_id'])][1]} pts"
           for t in standings if not t["deduction"]
           and [t["played"], t["points"]] != mine[(t["league"], t["team_id"])]])

    detailed = defaultdict(list)
    for p in players:
        detailed[p["match_id"]].append(p)
    old = (today - timedelta(days=2)).isoformat()
    check("D1", "warn", "Match played 2+ days ago without player details",
          [f"{m['match_id']}{' (unusable)' if m['match_id'] in unusable else ''}" for m in matches
           if m["match_id"] not in detailed and (m["date"] or "") <= old])

    def goals_differ(m):
        rows = detailed.get(m["match_id"])
        if not rows:
            return False
        h = sum((p["goals"] or 0) if p["team_id"] == m["home_team_id"] else (p["own_goals"] or 0) for p in rows)
        a = sum((p["goals"] or 0) if p["team_id"] == m["away_team_id"] else (p["own_goals"] or 0) for p in rows)
        return (h, a) != (m["home_goals"], m["away_goals"])
    check("D2", "warn", "Score differs from players' goals + own goals",
          [m["match_id"] for m in matches if goals_differ(m)])

    shot_goals, with_shots, shot_xg = Counter(), set(), Counter()
    for x in shots:
        with_shots.add(x["match_id"])
        if not x["own_goal"]:
            shot_xg[(x["match_id"], x["team_id"])] += x["xg"] or 0
            if x["result"] == "Goal":
                shot_goals[(x["match_id"], x["player_id"])] += 1
    check("D3", "warn", "Player's goals differ from his goals in the shot data",
          [f"{p['player']} (match {p['match_id']})" for p in players if p["match_id"] in with_shots
           and (p["goals"] or 0) != shot_goals.get((p["match_id"], p["player_id"]), 0)])

    starters = Counter((p["match_id"], p["team_id"]) for p in players if p["started"])
    check("D4", "warn", "Team without exactly 11 starters",
          [f"match {mid} team {tid}: {n}" for (mid, tid), n in starters.items() if n != 11])

    # the other way round is common (7 of 1,000 team rows on real data), probably because the
    # team figure counts several shots of one attack once
    check("D5", "warn", "Team xG higher than the sum of its shots' xG by more than 0.3 (shots missing?)",
          [f"{t['team']} match {t['match_id']}: {t['xg']} vs {shot_xg[(t['match_id'], t['team_id'])]:.2f}"
           for t in team_stats if t["xg"] is not None and t["match_id"] in with_shots
           and t["xg"] - shot_xg[(t["match_id"], t["team_id"])] > 0.3])

    # ---- the whole history: is (almost) every match of every season there?
    cov = coverage(wh, today, names)
    label = lambda r: f"{r['league']} {config.season_label(r['season'])}"
    check("H1", "error", "League-season whose list of matches is shorter than teams x (teams - 1)",
          [f"{label(r)}: {r['listed']} listed, {r['expected']} expected" for r in cov
           if r["listed"] < r["expected"]])
    check("H2", "error", f"Finished league-season with less than {MIN_COVERAGE:.0%} of its matches "
          f"played, or less than {MIN_COVERAGE:.0%} of its matches with details",
          [f"{label(r)}: {r['played']}/{r['expected']} played, {r['settled_details']}/{r['settled']} with details"
           for r in cov if r["finished"] and min(r["share_played"], r["share_details"]) < MIN_COVERAGE])
    check("H3", "warn", "Played match without a download of its details (2+ days old)",
          [f"{label(r)}: match {mid}" for r in cov for mid in r["pending"]])
    check("H4", "warn", f"League-season with more than {MAX_XG_MISSING:.0%} of detailed matches "
          f"without xG",
          [f"{label(r)}: {r['no_xg']}/{r['details']}" for r in cov
           if r["details"] and r["no_xg"] / r["details"] > MAX_XG_MISSING])
    total = sum(r["played"] for r in cov)
    gaps = sum(len(r["unusable"]) + len(r["pending"]) for r in cov)
    log(f"  history: {len(cov)} league-seasons, {total} played matches, "
        f"{sum(r['details'] for r in cov)} with details, {gaps} without "
        f"({sum(len(r['unusable']) for r in cov)} that FotMob cannot give)")

    check("X1", "warn", "Fixtures postponed (kick-off passed 2+ days ago, no result) or cancelled",
          [f"{f['home_team']} v {f['away_team']} {f['date']} ({f['status']})" for f in fixtures
           if f["status"] in ("postponed", "cancelled")])

    wh.append("core.quality_checks", results)
    errors = [r for r in results if r["status"] == "error"]
    for r in results:
        if r["status"] != "ok":
            log(f"  {r['status'].upper():<5} {r['check_id']} {r['description']}: {r['row_count']} "
                f"({r['sample']})")
    log(f"  checks: {len(results)} run, {len(errors)} errors, "
        f"{sum(r['status'] == 'warn' for r in results)} warnings")
    return results, len(errors)

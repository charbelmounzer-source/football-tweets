"""FotMob answers -> table rows. Pure functions: no network, no database, easy to test.

Two steps, for a league-season and for a match:
  slim_*()    keeps the parts of FotMob's answer that are used (a match answer goes from
              ~400 kB to ~40 kB). The slim copy is what is stored in raw.* and what is hashed
              to see whether FotMob changed something.
  model_*()   slim copy -> rows of the core tables. Tables can be rebuilt from raw.* alone.

Field paths were read on real answers on 11 November 2026 (Premier League 2026/27).
"""

import re
import unicodedata
from datetime import datetime, timedelta

from . import config
from .config import fotmob_season, season_label
from .schemas import PLAYER_MATCHES as PLAYER_COLUMNS

SCORE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")
SHOT_FIELDS = ("id", "eventType", "teamId", "playerId", "playerName", "x", "y", "min", "minAdded",
               "period", "isBlocked", "isOnTarget", "isOwnGoal", "isFromInsideBox",
               "expectedGoals", "expectedGoalsOnTarget", "shotType", "situation")


# ------------------------------------------------------------------ small helpers
def num(x, cast=float):
    """'3.55' -> 3.55, '438 (87%)' -> 438, None/'' -> None."""
    if isinstance(x, str):
        x = x.split("(")[0].strip()
    try:
        return int(float(x)) if cast is int else float(x)
    except (TypeError, ValueError):
        return None


def flag(x):
    return None if x is None else int(bool(x))


def iso_dt(s):
    """'2026-08-21T19:00:00.000Z' -> '2026-08-21 19:00:00' (UTC)."""
    if not s:
        return None
    try:
        return datetime.strptime(str(s).replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S").strftime(
            "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def score(text):
    m = SCORE.match(text or "")
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def hda(h, a):
    return "H" if h > a else "D" if h == a else "A"


def wdl(f, a):
    return "W" if f > a else "D" if f == a else "L"


def team(t):
    t = t or {}
    return {"id": num(t.get("id"), int), "name": t.get("name")}


# ------------------------------------------------------------------ league-season
def slim_league(payload):
    payload = payload if isinstance(payload, dict) else {}
    details = payload.get("details") or {}
    matches = []
    for m in (payload.get("fixtures") or {}).get("allMatches") or []:
        st = m.get("status") or {}
        matches.append({
            "id": num(m.get("id"), int), "round": None if m.get("round") is None else str(m["round"]),
            "kickoff": st.get("utcTime"), "home": team(m.get("home")), "away": team(m.get("away")),
            "finished": bool(st.get("finished")), "started": bool(st.get("started")),
            "cancelled": bool(st.get("cancelled")), "awarded": bool(st.get("awarded")),
            "score": st.get("scoreStr"), "reason": (st.get("reason") or {}).get("longKey")})
    tables = payload.get("table") or []
    table = ((tables[0].get("data") or {}).get("table") or {}) if tables else {}
    xg = {num(r.get("id"), int): r for r in table.get("xg") or []}
    rows = []
    for r in table.get("all") or []:
        tid = num(r.get("id"), int)
        x = xg.get(tid) or {}
        rows.append({"id": tid, "name": r.get("name"), "position": r.get("idx"),
                     "played": r.get("played"), "wins": r.get("wins"), "draws": r.get("draws"),
                     "losses": r.get("losses"), "score": r.get("scoresStr"), "pts": r.get("pts"),
                     "deduction": r.get("deduction"), "xg": x.get("xg"), "xga": x.get("xgConceded"),
                     "xpts": x.get("xPoints")})
    return {"league_id": details.get("id"), "season": details.get("selectedSeason"),
            "matches": matches, "table": rows}


def validate_league(slim, league_id, season):
    """Reason the answer can't be trusted, or None. Stops an empty answer, or the answer for
    another season, from replacing a good league-season."""
    if slim.get("league_id") != league_id:
        return f"answer is for league {slim.get('league_id')!r}, not {league_id}"
    if slim.get("season") != fotmob_season(season):
        return f"answer is for season {slim.get('season')!r}, not {fotmob_season(season)}"
    if not slim.get("matches"):
        return "no matches in the answer"
    if any(m["id"] is None or m["home"]["id"] is None or m["away"]["id"] is None
           for m in slim["matches"]):
        return "a match without an id or a team"
    return None


def model_league(league, season, slim, now_utc):
    """now_utc: datetime (naive, UTC), used only for the status of matches without a result."""
    label = season_label(season)
    now = now_utc.strftime("%Y-%m-%d %H:%M:%S")
    recently = (now_utc - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
    matches, fixtures, team_matches, seen = [], [], [], set()
    for m in slim["matches"]:
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        kickoff = iso_dt(m["kickoff"])
        h, a = m["home"], m["away"]
        base = {"match_id": m["id"], "league": league, "season": season, "season_label": label,
                "date": kickoff[:10] if kickoff else None, "kickoff_utc": kickoff, "round": m["round"],
                "home_team_id": h["id"], "home_team": h["name"],
                "away_team_id": a["id"], "away_team": a["name"]}
        hg, ag = score(m["score"])
        if m["cancelled"]:
            # abandoned or called off; if it is replayed, FotMob lists the replay as another match
            fixtures.append(dict(base, status="cancelled"))
        elif m["finished"] and hg is not None:
            matches.append(dict(base, home_goals=hg, away_goals=ag, result=hda(hg, ag)))
            for side, me, opp, gf, ga in (("home", h, a, hg, ag), ("away", a, h, ag, hg)):
                res = wdl(gf, ga)
                team_matches.append({
                    "match_id": m["id"], "league": league, "season": season, "season_label": label,
                    "date": base["date"], "kickoff_utc": kickoff, "team_id": me["id"],
                    "team": me["name"], "home_away": side, "opponent_id": opp["id"],
                    "opponent": opp["name"], "goals_for": gf, "goals_against": ga, "result": res,
                    "points": {"W": 3, "D": 1, "L": 0}[res]})
        else:
            # no result: not kicked off, just played (the score can lag a little), or postponed
            status = ("upcoming" if not kickoff or kickoff > now
                      else "awaiting_result" if kickoff > recently else "postponed")
            fixtures.append(dict(base, status=status))

    standings = []
    for r in slim.get("table") or []:
        gf, ga = score(r.get("score"))
        standings.append({
            "league": league, "season": season, "season_label": label, "team_id": r["id"],
            "team": r["name"], "position": num(r["position"], int), "played": num(r["played"], int),
            "wins": num(r["wins"], int), "draws": num(r["draws"], int), "losses": num(r["losses"], int),
            "goals_for": gf, "goals_against": ga, "points": num(r["pts"], int),
            "deduction": None if r.get("deduction") in (None, "") else str(r["deduction"]),
            "xg": num(r.get("xg")), "xga": num(r.get("xga")), "xpts": num(r.get("xpts"))})
    return {"matches": matches, "fixtures": fixtures, "team_matches": team_matches,
            "standings": standings}


# ------------------------------------------------------------------ match
def slim_match(payload):
    payload = payload if isinstance(payload, dict) else {}
    g, header, c = payload.get("general") or {}, payload.get("header") or {}, payload.get("content") or {}
    scores = {num(t.get("id"), int): t.get("score") for t in header.get("teams") or []}
    status = header.get("status") or {}

    team_stats = {}
    periods = ((c.get("stats") or {}).get("Periods") or {}).get("All") or {}
    for group in periods.get("stats") or []:
        for s in group.get("stats") or []:
            values = s.get("stats") or []
            if s.get("key") and len(values) == 2 and values != [None, None]:
                team_stats[s["key"]] = values

    events = ((c.get("matchFacts") or {}).get("events") or {}).get("events") or []
    cards = [{"player_id": e.get("playerId"), "player": e.get("nameStr"), "card": e.get("card"),
              "minute": e.get("time"), "is_home": e.get("isHome")}
             for e in events if e.get("type") == "Card"]
    # Goal events are kept for matches without player stats (before 2016/17): there they are
    # the only record of who scored. Where player stats or shots exist, those are used instead:
    # the event list sometimes credits a scorer where the shot data records an own goal.
    goals = [{"player_id": e.get("playerId"), "player": e.get("nameStr") or e.get("fullName"),
              "is_home": e.get("isHome"), "own_goal": bool(e.get("ownGoal")), "minute": e.get("time")}
             for e in events if e.get("type") == "Goal" and not e.get("isPenaltyShootoutEvent")]
    # who came on: a substitution names both players (the one going off started anyway)
    swaps = [[num(x.get("id"), int) for x in e.get("swap") or []]
             for e in events if e.get("type") == "Substitution"]

    lineup = {}
    for side, key in (("home", "homeTeam"), ("away", "awayTeam")):
        t = (c.get("lineup") or {}).get(key) or {}
        everyone = (t.get("starters") or []) + (t.get("subs") or [])
        lineup[side] = {"formation": t.get("formation"),
                        "starters": [p.get("id") for p in t.get("starters") or []],
                        "subs": [p.get("id") for p in t.get("subs") or []],
                        "names": [[p.get("id"), p.get("name")] for p in everyone]}

    players = []
    for p in (c.get("playerStats") or {}).values():
        stats = {}
        for group in p.get("stats") or []:
            for s in (group.get("stats") or {}).values():
                st = s.get("stat") or {}
                if s.get("key") and st.get("value") is not None:
                    stats[s["key"]] = [st["value"], st["total"]] if "total" in st else st["value"]
        if stats:                                           # unused substitutes have no stats
            players.append({"id": p.get("id"), "name": p.get("name"), "team_id": p.get("teamId"),
                            "goalkeeper": bool(p.get("isGoalkeeper")), "stats": stats})

    shots = [{k: s.get(k) for k in SHOT_FIELDS} for s in (c.get("shotmap") or {}).get("shots") or []]
    home, away = team(g.get("homeTeam")), team(g.get("awayTeam"))
    return {"match_id": num(g.get("matchId"), int), "league_id": g.get("leagueId"),
            "kickoff": g.get("matchTimeUTCDate"), "coverage": g.get("coverageLevel"),
            "home": dict(home, score=scores.get(home["id"])),
            "away": dict(away, score=scores.get(away["id"])),
            "finished": bool(status.get("finished")), "cancelled": bool(status.get("cancelled")),
            "team_stats": team_stats, "cards": cards, "goals": goals, "swaps": swaps,
            "lineup": lineup, "players": players, "shots": shots}


def validate_match(slim, match):
    """Reason the details can't be used (yet), or None."""
    if slim.get("match_id") is None:
        return "empty page"                      # some 2010/11 matches: FotMob has nothing
    if slim.get("match_id") != match["match_id"]:
        return f"answer is for match {slim.get('match_id')!r}"
    if (slim["home"]["id"], slim["away"]["id"]) != (match["home_team_id"], match["away_team_id"]):
        return "teams differ from the league data"
    if not slim["finished"]:
        return "not finished according to the match answer"
    if match["season"] >= config.PLAYER_STATS_FROM and not has_player_stats(slim):
        return "no player stats"
    if not has_player_stats(slim) and not has_lineups(slim):
        return "no lineup"                       # before 2016/17: a lineup is the least we use
    return None


def has_player_stats(slim):
    return {slim["home"]["id"], slim["away"]["id"]} <= {p["team_id"] for p in slim["players"]}


def has_lineups(slim):
    return all(len(slim["lineup"][side]["starters"]) == 11 for side in ("home", "away"))


def stat(p, key, cast=float):
    v = p["stats"].get(key)
    return num(v[0] if isinstance(v, list) else v, cast)


# player counts FotMob leaves out when they are zero: column -> FotMob key. For "won" stats
# FotMob gives [won, attempted]; the first number is kept.
OUTFIELD_COUNTS = {
    "big_chances_created": "big_chance_created_team_title", "big_chances_missed": "big_chance_missed_title",
    "touches_in_box": "touches_opp_box", "dribbles": "dribbles_succeeded",
    "tackles": "matchstats.headers.tackles", "interceptions": "interceptions",
    "recoveries": "recoveries", "duels_won": "duel_won", "aerials_won": "aerials_won",
    "passes_final_third": "passes_into_final_third",
}


def keeper_stats(p, zero):
    """Goalkeeping columns: NULL for outfield players. xGOT faced and goals prevented are left
    NULL when FotMob does not give them (a keeper who faced no shot on target)."""
    if not p.get("goalkeeper"):
        return {"saves": None, "goals_conceded": None, "xgot_faced": None, "goals_prevented": None}
    saves, conceded = stat(p, "saves", int), stat(p, "goals_conceded", int)
    return {"saves": zero if saves is None else saves,
            "goals_conceded": zero if conceded is None else conceded,
            "xgot_faced": stat(p, "expected_goals_on_target_faced"),
            "goals_prevented": stat(p, "goals_prevented")}


def model_match(match, slim):
    """match: the row of core.matches for this match (gives league, season, date, team names).
    A match with player stats is modelled from them; an older one (before 2016/17) from its
    lineups and events, with NULL for everything those do not give."""
    if not has_player_stats(slim):
        return model_lineups(match, slim)
    keys = {k: match[k] for k in ("match_id", "league", "season", "season_label", "date", "kickoff_utc")}
    home_id, away_id = match["home_team_id"], match["away_team_id"]
    names = {home_id: match["home_team"], away_id: match["away_team"]}
    side_of = {home_id: "home", away_id: "away"}

    shots, by_player, by_team = {}, {}, {home_id: {"goals": 0}, away_id: {"goals": 0}}
    for s in slim["shots"]:
        pid, tid, xg = s.get("playerId"), s.get("teamId"), num(s.get("expectedGoals"))
        goal, pen, own = s.get("eventType") == "Goal", s.get("situation") == "Penalty", bool(s.get("isOwnGoal"))
        a = by_player.setdefault(pid, {"pens": 0, "pen_xg": 0.0, "own": 0})
        a["pens"] += goal and pen and not own
        a["own"] += goal and own
        a["pen_xg"] += (xg or 0) if pen else 0
        if tid in by_team:
            by_team[tid]["goals"] += goal and not own
        shots[s.get("id")] = {
            "shot_id": s.get("id"), "match_id": keys["match_id"], "league": keys["league"],
            "season": keys["season"], "date": keys["date"], "minute": num(s.get("min"), int),
            "minute_added": num(s.get("minAdded"), int), "period": s.get("period"),
            "player_id": pid, "player": s.get("playerName"), "team_id": tid, "team": names.get(tid),
            "home_away": side_of.get(tid), "result": s.get("eventType"),
            "on_target": flag(s.get("isOnTarget")), "blocked": flag(s.get("isBlocked")),
            "own_goal": flag(s.get("isOwnGoal")), "xg": xg, "xgot": num(s.get("expectedGoalsOnTarget")),
            "x": num(s.get("x")), "y": num(s.get("y")), "inside_box": flag(s.get("isFromInsideBox")),
            "situation": s.get("situation"), "shot_type": s.get("shotType")}
    has_shots = bool(shots)

    ts = slim["team_stats"]

    def pair(key, cast=float):
        v = ts.get(key) or [None, None]
        return {home_id: num(v[0], cast), away_id: num(v[1], cast)}
    xg, npxg = pair("expected_goals"), pair("expected_goals_non_penalty")
    xgot, n_shots, sot = pair("expected_goals_on_target"), pair("total_shots", int), pair("ShotsOnTarget", int)
    big, poss = pair("big_chance", int), pair("BallPossesion", int)
    corners, box = pair("corners", int), pair("touches_opp_box", int)
    big_missed, saves = pair("big_chance_missed_title", int), pair("keeper_saves", int)
    xg_open, xg_set = pair("expected_goals_open_play"), pair("expected_goals_set_play")
    team_stats = []
    for tid, opp in ((home_id, away_id), (away_id, home_id)):
        team_stats.append(dict(
            keys, team_id=tid, team=names[tid], home_away=side_of[tid], xg=xg[tid], xga=xg[opp],
            npxg=npxg[tid], npxga=npxg[opp], xgot=xgot[tid], shots=n_shots[tid],
            shots_on_target=sot[tid], shot_goals=int(by_team[tid]["goals"]) if has_shots else None,
            big_chances=big[tid], possession=poss[tid], corners=corners[tid], touches_in_box=box[tid],
            big_chances_missed=big_missed[tid], xg_open_play=xg_open[tid], xg_set_play=xg_set[tid],
            keeper_saves=saves[tid]))

    yellow, red = {}, {}
    for e in slim["cards"]:
        pid, card = e.get("player_id"), (e.get("card") or "").lower()
        if "red" in card:                                   # 'Red' and a second yellow ('YellowRed')
            red[pid] = 1
        elif card == "yellow":
            yellow[pid] = yellow.get(pid, 0) + 1
    starters = set(slim["lineup"]["home"]["starters"]) | set(slim["lineup"]["away"]["starters"])

    players = {}
    for p in slim["players"]:
        pid, tid = p["id"], p["team_id"]
        if tid not in names:
            continue
        a = by_player.get(pid, {"pens": 0, "pen_xg": 0.0, "own": 0})
        # FotMob leaves a stat out when it is zero (no shot -> no "total_shots", no xG).
        # With shot data for the match, a missing value is a real zero; without, it is unknown.
        zero, zero_f = (0, 0.0) if has_shots else (None, None)
        p_xg, p_npxg = stat(p, "expected_goals"), stat(p, "expected_goals_non_penalty")
        p_xg = zero_f if p_xg is None else p_xg
        if p_npxg is None and p_xg is not None:
            p_npxg = round(max(p_xg - a["pen_xg"], 0.0), 2)

        def or_zero(v, z=zero):
            return z if v is None else v
        players[pid] = dict(
            keys, player_id=pid, player=p["name"], team_id=tid, team=names[tid],
            home_away=side_of[tid], started=int(pid in starters) if starters else None,
            minutes=stat(p, "minutes_played", int), goals=stat(p, "goals", int) or 0,
            penalty_goals=int(a["pens"]) if has_shots else None,
            own_goals=int(a["own"]) if has_shots else None,
            assists=stat(p, "assists", int) or 0, shots=or_zero(stat(p, "total_shots", int)),
            shots_on_target=or_zero(stat(p, "ShotsOnTarget", int)), xg=p_xg, npxg=p_npxg,
            xgot=or_zero(stat(p, "expected_goals_on_target_variant"), zero_f),
            xa=or_zero(stat(p, "expected_assists"), zero_f),
            key_passes=or_zero(stat(p, "chances_created", int)), touches=stat(p, "touches", int),
            yellow=yellow.get(pid, 0), red=red.get(pid, 0), rating=stat(p, "rating_title"),
            **{col: or_zero(stat(p, key, int)) for col, key in OUTFIELD_COUNTS.items()},
            **keeper_stats(p, zero))
    return {"team_stats": team_stats, "player_matches": list(players.values()),
            "shots": list(shots.values())}


# ------------------------------------------------------------------ older matches: lineups + events
def plain(name):
    """'Kylian Mbappé ' -> 'kylian mbappe': names in events and lineups are compared this way."""
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    return " ".join(s.lower().replace("-", " ").replace(".", " ").split())


def name_finder(people):
    """people: [(id, name)] -> function(name) -> id or None. Exact (accent-free) name first,
    then a surname that is unique among these people."""
    exact, last = {}, {}
    for pid, name in people:
        exact.setdefault(plain(name), set()).add(pid)
        last.setdefault(plain(name).split(" ")[-1] if plain(name) else "", set()).add(pid)

    def find(name):
        n = plain(name)
        if not n:
            return None
        ids = exact.get(n) or last.get(n.split(" ")[-1])
        return next(iter(ids)) if ids and len(ids) == 1 else None
    return find


def event_goals(slim, home_id, away_id, hg, ag, find):
    """{player_id: [goals, own_goals]} from the goal events, or None when they cannot be
    trusted: a scorer not found in the lineups, or a total that is not the final score."""
    found = {}
    by_side = {True: 0, False: 0}
    for e in slim.get("goals") or []:
        side = e.get("is_home")
        if side not in (True, False):
            return None
        pid = find[side](e.get("player")) if not e["own_goal"] else (
            find[not side](e.get("player")) or find[side](e.get("player")))
        if pid is None:
            return None
        by_side[side] += 1
        f = found.setdefault(pid, [0, 0])
        f[1 if e["own_goal"] else 0] += 1
    return found if (by_side[True], by_side[False]) == (hg, ag) else None


def model_lineups(match, slim):
    keys = {k: match[k] for k in ("match_id", "league", "season", "season_label", "date", "kickoff_utc")}
    home_id, away_id = match["home_team_id"], match["away_team_id"]
    names = {home_id: match["home_team"], away_id: match["away_team"]}
    side_id = {"home": home_id, "away": away_id}
    team_stats = [dict(keys, team_id=tid, team=names[tid], home_away=side,
                       **{c: None for c in ("xg", "xga", "npxg", "npxga", "xgot", "shots",
                                            "shots_on_target", "shot_goals", "big_chances", "possession",
                                            "corners", "touches_in_box", "big_chances_missed",
                                            "xg_open_play", "xg_set_play", "keeper_saves")})
                  for side, tid in side_id.items()]
    came_on = {pid for swap in slim.get("swaps") or [] for pid in swap if pid is not None}
    people, find, team_of, who = {}, {}, {}, {}
    for side in ("home", "away"):
        lu = slim["lineup"][side]
        named = {num(pid, int): name for pid, name in lu.get("names") or []}
        starters = {num(p, int) for p in lu["starters"]}
        played = starters | ({num(p, int) for p in lu["subs"]} & came_on)
        people[side] = [(pid, named.get(pid)) for pid in played]
        find[side == "home"] = name_finder(people[side])
        for pid, name in people[side]:
            team_of[pid], who[pid] = side_id[side], (name, pid in starters)
    goals = event_goals(slim, home_id, away_id, match["home_goals"], match["away_goals"], find)
    # cards: each must be found in a lineup, or cards are unknown for the whole match
    yellow, red, cards_ok = {}, {}, True
    for e in slim.get("cards") or []:
        side = e.get("is_home")
        pid = find[side](e.get("player")) if side in (True, False) else None
        if pid is None:
            cards_ok = False
            break
        card = (e.get("card") or "").lower()
        if "red" in card:
            red[pid] = 1
        elif card == "yellow":
            yellow[pid] = yellow.get(pid, 0) + 1
    players = []
    for pid, tid in team_of.items():
        name, started = who[pid]
        g = (goals or {}).get(pid, [0, 0])
        row = {c: None for c, _t in PLAYER_COLUMNS}
        row.update(keys, player_id=pid, player=name, team_id=tid, team=names[tid],
                   home_away="home" if tid == home_id else "away", started=int(started),
                   goals=g[0] if goals is not None else None,
                   own_goals=g[1] if goals is not None else None,
                   yellow=yellow.get(pid, 0) if cards_ok else None,
                   red=red.get(pid, 0) if cards_ok else None)
        players.append(row)
    return {"team_stats": team_stats, "player_matches": players, "shots": []}

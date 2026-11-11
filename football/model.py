"""Understat payloads -> table rows. Pure functions: no network, no database, easy to test.

model_league(): one league-season payload -> matches, fixtures, team_matches, player_seasons
model_match():  one match payload         -> player_matches, shots
"""

from datetime import datetime, timedelta

from .config import season_label


def num(x, cast=float):
    try:
        return cast(float(x)) if cast is int else cast(x)
    except (TypeError, ValueError):
        return None


def iso_dt(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s).replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S").strftime(
            "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def hda(h, a):
    if h is None or a is None:
        return None
    return "H" if h > a else "D" if h == a else "A"


def wdl(f, a):
    if f is None or a is None:
        return None
    return "W" if f > a else "D" if f == a else "L"


def validate_league(payload):
    """Reason the payload can't be trusted, or None. Stops an empty or half-broken answer
    from wiping a good league-season."""
    if not isinstance(payload, dict):
        return "not a JSON object"
    dates = payload.get("dates")
    if not isinstance(dates, list) or not dates:
        return "no matches ('dates' is empty)"
    if any(m.get("isResult") for m in dates) and not payload.get("teams"):
        return "results but no team stats"
    return None


def validate_match(payload):
    if not isinstance(payload, dict):
        return "not a JSON object"
    rosters = payload.get("rosters") or {}
    if not rosters.get("h") or not rosters.get("a"):
        return "missing rosters"
    return None


def model_league(league, season, payload, now_utc):
    """now_utc: datetime (naive, UTC), used only for the fixture status."""
    label = season_label(season)
    now = now_utc.strftime("%Y-%m-%d %H:%M:%S")
    recently = (now_utc - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
    matches, fixtures, by_kickoff = [], [], {}
    for m in payload.get("dates", []):
        mid = num(m.get("id"), int)
        kickoff = iso_dt(m.get("datetime"))
        h, a, fc = m.get("h") or {}, m.get("a") or {}, m.get("forecast") or {}
        base = {
            "match_id": mid, "league": league, "season": season, "season_label": label,
            "date": kickoff[:10] if kickoff else None, "kickoff_utc": kickoff,
            "home_team_id": num(h.get("id"), int), "home_team": h.get("title"),
            "away_team_id": num(a.get("id"), int), "away_team": a.get("title"),
            "forecast_home_win": num(fc.get("w")), "forecast_draw": num(fc.get("d")),
            "forecast_away_win": num(fc.get("l")),
        }
        if m.get("isResult"):
            hg, ag = num((m.get("goals") or {}).get("h"), int), num((m.get("goals") or {}).get("a"), int)
            row = dict(base, home_goals=hg, away_goals=ag, result=hda(hg, ag),
                       home_xg=num((m.get("xG") or {}).get("h")), away_xg=num((m.get("xG") or {}).get("a")))
            matches.append(row)
            by_kickoff[(kickoff, row["home_team_id"])] = row
            by_kickoff[(kickoff, row["away_team_id"])] = row
        else:
            # no result yet: not kicked off, just played (Understat lags a little), or postponed
            status = ("upcoming" if not kickoff or kickoff > now
                      else "awaiting_result" if kickoff > recently else "postponed")
            fixtures.append(dict(base, status=status))

    team_matches = []
    for t in (payload.get("teams") or {}).values():
        tid = num(t.get("id"), int)
        for h in t.get("history", []):
            kickoff = iso_dt(h.get("date"))
            m = by_kickoff.get((kickoff, tid))
            home = bool(m) and m["home_team_id"] == tid
            ppda, ppda_a = h.get("ppda") or {}, h.get("ppda_allowed") or {}
            gf, ga = num(h.get("scored"), int), num(h.get("missed"), int)
            team_matches.append({
                "match_id": m["match_id"] if m else None, "league": league, "season": season,
                "season_label": label, "date": kickoff[:10] if kickoff else None, "kickoff_utc": kickoff,
                "team_id": tid, "team": t.get("title"),
                "home_away": {"h": "home", "a": "away"}.get(h.get("h_a")),
                "opponent_id": (m["away_team_id"] if home else m["home_team_id"]) if m else None,
                "opponent": (m["away_team"] if home else m["home_team"]) if m else None,
                "goals_for": gf, "goals_against": ga, "result": wdl(gf, ga),
                "xg": num(h.get("xG")), "xga": num(h.get("xGA")),
                "npxg": num(h.get("npxG")), "npxga": num(h.get("npxGA")),
                "ppda": round(ppda["att"] / ppda["def"], 2) if ppda.get("def") else None,
                "ppda_allowed": round(ppda_a["att"] / ppda_a["def"], 2) if ppda_a.get("def") else None,
                "deep": num(h.get("deep"), int), "deep_allowed": num(h.get("deep_allowed"), int),
                "xpts": num(h.get("xpts")), "points": num(h.get("pts"), int),
            })

    player_seasons = {}
    for p in payload.get("players", []):
        goals, npg = num(p.get("goals"), int), num(p.get("npg"), int)
        pid = num(p.get("id"), int)
        player_seasons[pid] = {                              # keyed: one row per player
            "player_id": pid, "player": p.get("player_name"), "league": league, "season": season,
            "season_label": label, "teams": p.get("team_title"), "position": p.get("position"),
            "games": num(p.get("games"), int), "minutes": num(p.get("time"), int),
            "goals": goals, "non_penalty_goals": npg,
            "penalty_goals": (goals - npg) if goals is not None and npg is not None else None,
            "assists": num(p.get("assists"), int), "xg": num(p.get("xG")), "npxg": num(p.get("npxG")),
            "xa": num(p.get("xA")), "shots": num(p.get("shots"), int),
            "key_passes": num(p.get("key_passes"), int),
            "yellow": num(p.get("yellow_cards"), int), "red": num(p.get("red_cards"), int),
            "xg_chain": num(p.get("xGChain")), "xg_buildup": num(p.get("xGBuildup")),
        }
    return {"matches": matches, "fixtures": fixtures, "team_matches": team_matches,
            "player_seasons": list(player_seasons.values())}


def model_match(match, payload):
    """match: the row of core.matches for this match (gives league, date, team names)."""
    mid = match["match_id"]
    team_name = {match["home_team_id"]: match["home_team"], match["away_team_id"]: match["away_team"]}
    side_team = {"h": match["home_team_id"], "a": match["away_team_id"]}

    shots, agg = {}, {}
    roster_team = {}
    for side in ("h", "a"):
        for r in ((payload.get("rosters") or {}).get(side) or {}).values():
            roster_team[num(r.get("player_id"), int)] = num(r.get("team_id"), int)
    for side in ("h", "a"):
        for s in ((payload.get("shots") or {}).get(side) or []):
            pid, xg = num(s.get("player_id"), int), num(s.get("xG"))
            a = agg.setdefault(pid, {"sot": 0, "npxg": 0.0, "pens": 0})
            a["sot"] += s.get("result") in ("Goal", "SavedShot")
            a["npxg"] += (xg or 0) if s.get("situation") != "Penalty" else 0
            a["pens"] += s.get("result") == "Goal" and s.get("situation") == "Penalty"
            # the shooter's team comes from the roster; the shot's own flag is only a fallback
            tid = roster_team.get(pid) or side_team.get(s.get("h_a"))
            sid = num(s.get("id"), int)
            shots[sid] = {
                "shot_id": sid, "match_id": mid, "league": match["league"], "season": match["season"],
                "date": match["date"], "minute": num(s.get("minute"), int), "player_id": pid,
                "player": s.get("player"), "team_id": tid, "team": team_name.get(tid),
                "home_away_flag": {"h": "home", "a": "away"}.get(s.get("h_a")),
                "result": s.get("result"), "xg": xg, "x": num(s.get("X")), "y": num(s.get("Y")),
                "situation": s.get("situation"), "shot_type": s.get("shotType"),
                "last_action": s.get("lastAction"), "assist_player": s.get("player_assisted"),
            }
    has_shots = bool(shots)

    players = {}
    for side in ("h", "a"):
        for r in ((payload.get("rosters") or {}).get(side) or {}).values():
            pid, tid = num(r.get("player_id"), int), num(r.get("team_id"), int)
            a = agg.get(pid, {"sot": 0, "npxg": 0.0, "pens": 0})
            players[pid] = {
                "match_id": mid, "league": match["league"], "season": match["season"],
                "season_label": match["season_label"], "date": match["date"],
                "kickoff_utc": match["kickoff_utc"], "player_id": pid, "player": r.get("player"),
                "team_id": tid, "team": team_name.get(tid),
                "home_away": {"h": "home", "a": "away"}.get(r.get("h_a")),
                "match_position": r.get("position"), "started": 0 if r.get("position") == "Sub" else 1,
                "minutes": num(r.get("time"), int), "goals": num(r.get("goals"), int),
                "own_goals": num(r.get("own_goals"), int), "assists": num(r.get("assists"), int),
                "yellow": num(r.get("yellow_card"), int), "red": num(r.get("red_card"), int),
                "shots": num(r.get("shots"), int), "xg": num(r.get("xG")), "xa": num(r.get("xA")),
                "key_passes": num(r.get("key_passes"), int), "xg_chain": num(r.get("xGChain")),
                "xg_buildup": num(r.get("xGBuildup")),
                "shots_on_target": int(a["sot"]) if has_shots else None,
                "npxg": round(a["npxg"], 4) if has_shots else None,
                "penalty_goals": int(a["pens"]) if has_shots else None,
            }
    return {"player_matches": list(players.values()), "shots": list(shots.values())}

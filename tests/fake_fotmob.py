"""A small simulated FotMob: same JSON shapes as the real site (the parts the pipeline reads,
as seen on real answers on 11 November 2026), deterministic content.

10 teams per league, double round robin (18 rounds, one per week from 15 August), 12 players
per team and one unused substitute. Scores, shots, player stats and the league table are
consistent with each other, like the real data.
"""

import random
from datetime import datetime, timedelta

from football import config
from football.fotmob import BudgetSpent, FotMobError

TEAMS_PER_LEAGUE = 10
CODES = list(config.LEAGUES)


def round_robin(n):
    teams, rounds = list(range(n)), []
    for _ in range(n - 1):
        rounds.append([(teams[i], teams[n - 1 - i]) for i in range(n // 2)])
        teams = [teams[0]] + [teams[-1]] + teams[1:-1]
    return rounds + [[(b, a) for a, b in r] for r in rounds]


def stat_group(key, stats):
    """{'goals': 1} -> the nested shape FotMob uses for one group of player stats."""
    return {"title": key, "key": key,
            "stats": {f"title of {k}": {"key": k, "stat": {"value": v, "type": "integer"}}
                      for k, v in stats.items()}}


class FakeFotMob:
    def __init__(self, now, max_requests=100000):
        self.now, self.max_requests, self.used = now, max_requests, 0
        self.fail_leagues, self.fail_matches, self.tweak = set(), set(), {}
        self.calls = []
        self._matches = {}                      # match_id -> simulated match

    # ------------------------------------------------------------ simulation
    def _team(self, code, i):
        return CODES.index(code) * 100 + i + 1, f"{code} Team {i + 1:02d}"

    def _season(self, code, season):
        out = []
        for r, pairs in enumerate(round_robin(TEAMS_PER_LEAGUE)):
            kickoff = datetime(season, 8, 15, 15, 0) + timedelta(days=7 * r)
            for k, (h, a) in enumerate(pairs):
                mid = CODES.index(code) * 1_000_000 + season * 1000 + r * 10 + k
                out.append(self._match(mid, code, season, kickoff, h, a, r + 1))
        return out

    def _match(self, mid, code, season, kickoff, h, a, rnd_no):
        if mid in self._matches:
            return self._matches[mid]
        rnd = random.Random(mid)
        sides = {}
        for side, t in (("home", h), ("away", a)):
            tid, name = self._team(code, t)
            strength = 0.8 + 0.12 * (TEAMS_PER_LEAGUE - t)          # team 01 is the strongest
            players = [{"id": tid * 100 + n, "name": f"Player {tid}-{n:02d}", "teamId": tid,
                        "minutes": 20 if n == 11 else 70 if n == 10 else 90,
                        "goals": 0, "shots": 0, "sot": 0, "xg": 0.0} for n in range(12)]
            shots = []
            for _ in range(rnd.randint(6, 14)):
                p = players[rnd.choice([8, 9, 9, 10, 10, 10, 11])]
                xg = round(rnd.uniform(0.03, 0.35) * strength, 3)
                goal = rnd.random() < xg * 1.1
                result = "Goal" if goal else rnd.choice(["AttemptSaved", "Miss", "Miss"])
                shots.append({
                    "id": mid * 100 + len(shots) + (0 if side == "home" else 50), "eventType": result,
                    "teamId": tid, "playerId": p["id"], "playerName": p["name"], "x": 95.0, "y": 34.0,
                    "min": rnd.randint(1, 90), "minAdded": None, "period": "FirstHalf",
                    "isBlocked": False, "isOnTarget": result != "Miss", "isOwnGoal": False,
                    "isFromInsideBox": True, "expectedGoals": xg,
                    "expectedGoalsOnTarget": xg if result != "Miss" else 0,
                    "shotType": "RightFoot", "situation": "RegularPlay", "teamColor": "#000"})
                p["shots"] += 1
                p["sot"] += result != "Miss"
                p["xg"] = round(p["xg"] + xg, 3)
                p["goals"] += int(goal)
            sides[side] = {"tid": tid, "name": name, "players": players, "shots": shots,
                           "goals": sum(p["goals"] for p in players),
                           "xg": round(sum(s["expectedGoals"] for s in shots), 2)}
        m = {"id": mid, "code": code, "season": season, "kickoff": kickoff, "round": rnd_no, **sides}
        self._matches[mid] = m
        return m

    # ------------------------------------------------------------ the two endpoints
    def _count(self):
        if self.used >= self.max_requests:
            raise BudgetSpent()
        self.used += 1

    def league(self, code, season):
        self._count()
        self.calls.append(("league", code, season))
        if (code, season) in self.fail_leagues:
            raise FotMobError(f"leagues?id={config.FOTMOB_IDS[code]}: HTTP 503")
        if season > config.current_season(self.now.date()):
            return None
        fixtures, table = [], {}
        for m in self._season(code, season):
            played = m["kickoff"] + timedelta(hours=2) <= self.now
            h, a = m["home"], m["away"]
            status = {"utcTime": m["kickoff"].strftime("%Y-%m-%dT%H:%M:%SZ"), "timezone": "",
                      "started": played, "cancelled": False, "finished": played}
            if played:
                status.update(awarded=False, scoreStr=f"{h['goals']} - {a['goals']}",
                              reason={"short": "FT", "longKey": "finished"})
            fixtures.append({"round": str(m["round"]), "roundName": m["round"], "id": str(m["id"]),
                             "home": {"name": h["name"], "shortName": h["name"][:3], "id": str(h["tid"])},
                             "away": {"name": a["name"], "shortName": a["name"][:3], "id": str(a["tid"])},
                             "status": status})
            if not played:
                continue
            for me, opp in ((h, a), (a, h)):
                t = table.setdefault(me["tid"], {"name": me["name"], "id": me["tid"], "played": 0,
                                                 "wins": 0, "draws": 0, "losses": 0, "gf": 0, "ga": 0,
                                                 "pts": 0, "xg": 0.0, "xga": 0.0, "deduction": None})
                gf, ga = me["goals"], opp["goals"]
                t["played"] += 1
                t["wins" if gf > ga else "draws" if gf == ga else "losses"] += 1
                t["gf"] += gf
                t["ga"] += ga
                t["pts"] += 3 if gf > ga else 1 if gf == ga else 0
                t["xg"] += me["xg"]
                t["xga"] += opp["xg"]
        ranked = sorted(table.values(), key=lambda t: (-t["pts"], t["id"]))
        rows = [{"name": t["name"], "id": t["id"], "deduction": t["deduction"], "played": t["played"],
                 "wins": t["wins"], "draws": t["draws"], "losses": t["losses"],
                 "scoresStr": f"{t['gf']}-{t['ga']}", "pts": t["pts"], "idx": i}
                for i, t in enumerate(ranked, 1)]
        xg = [{"id": t["id"], "teamId": t["id"], "xg": t["xg"], "xgConceded": t["xga"],
               "xPoints": round(1.35 * t["played"] + (t["xg"] - t["xga"]) / 2, 3)} for t in ranked]
        payload = {"details": {"id": config.FOTMOB_IDS[code], "name": config.LEAGUES[code],
                               "selectedSeason": config.fotmob_season(season)},
                   "allAvailableSeasons": [config.fotmob_season(season)],
                   "fixtures": {"allMatches": fixtures},
                   "table": [{"data": {"leagueName": config.LEAGUES[code], "composite": False,
                                       "table": {"all": rows, "home": [], "away": [], "xg": xg}}}],
                   "stats": {"players": [], "teams": []}, "transfers": {"data": []}}
        return self.tweak.get(("league", code, season), lambda x: x)(payload)

    def match(self, match_id):
        self._count()
        self.calls.append(("match", match_id))
        if match_id in self.fail_matches:
            raise FotMobError(f"matchDetails?matchId={match_id}: HTTP 503")
        if match_id not in self._matches:                  # ids encode league and season
            idx, season = match_id // 1_000_000, match_id % 1_000_000 // 1000
            if idx < len(CODES):
                self._season(CODES[idx], season)
        m = self._matches.get(match_id)
        if m is None:
            return None
        h, a = m["home"], m["away"]
        extra = random.Random(m["id"] + 7)          # separate stream: the scores above stay as they were
        player_stats, lineup, events = {}, {}, []
        for key, side in (("homeTeam", h), ("awayTeam", a)):
            opp = a if side is h else h
            assists = {}
            for p in side["players"]:                       # most goals get an assist
                for _ in range(p["goals"]):
                    if extra.random() < 0.7:
                        giver = side["players"][extra.choice([5, 6, 7, 8, 9, 10])]
                        if giver is not p:
                            assists[giver["id"]] = assists.get(giver["id"], 0) + 1
            for n, p in enumerate(side["players"]):
                keeper = n == 0
                top = {"rating_title": 6.5, "minutes_played": p["minutes"], "goals": p["goals"],
                       "assists": assists.get(p["id"], 0)}
                attack = {"touches": 40, "touches_opp_box": extra.randint(0, 6) if n >= 5 else 0}
                defence = {"matchstats.headers.tackles": extra.randint(0, 4),
                           "recoveries": extra.randint(1, 8), "interceptions": extra.randint(0, 3)}
                if p["shots"]:                              # like FotMob: zero stats are left out
                    top.update(total_shots=p["shots"], expected_goals=p["xg"], ShotsOnTarget=p["sot"])
                    attack["expected_goals_non_penalty"] = p["xg"]
                if n >= 5:
                    attack["big_chance_created_team_title"] = extra.choice([0, 0, 0, 1, 1, 2])
                    attack["dribbles_succeeded"] = [extra.randint(0, 4), 5]
                    attack["expected_assists"] = round(extra.uniform(0, 0.4), 2)
                    attack["chances_created"] = extra.randint(0, 4)
                groups = [stat_group("top_stats", {k: v for k, v in top.items() if v != 0 or k == "goals"}),
                          stat_group("attack", {k: v for k, v in attack.items() if v != 0}),
                          stat_group("defense", {k: v for k, v in defence.items() if v != 0})]
                if keeper:
                    on_target = sum(x["isOnTarget"] for x in opp["shots"])
                    faced = round(sum(x["expectedGoalsOnTarget"] for x in opp["shots"]), 2)
                    groups.append(stat_group("goalkeeping", {
                        "saves": max(on_target - opp["goals"], 0), "goals_conceded": opp["goals"],
                        "expected_goals_on_target_faced": faced,
                        "goals_prevented": round(faced - opp["goals"], 2)}))
                player_stats[str(p["id"])] = {
                    "name": p["name"], "id": p["id"], "teamId": p["teamId"], "teamName": side["name"],
                    "isGoalkeeper": keeper, "stats": groups,
                    "shotmap": [s for s in side["shots"] if s["playerId"] == p["id"]], "funFacts": []}
                events += [{"type": "Goal", "time": 10, "overloadTime": None, "isHome": side is h,
                            "ownGoal": None, "goalDescription": None, "nameStr": p["name"],
                            "playerId": p["id"], "isPenaltyShootoutEvent": False}] * p["goals"]
            unused = side["tid"] * 100 + 99                 # on the bench, never came on
            player_stats[str(unused)] = {"name": "Unused Sub", "id": unused, "teamId": side["tid"],
                                         "isGoalkeeper": True, "stats": []}
            lineup[key] = {"id": side["tid"], "name": side["name"], "formation": "4-4-2",
                           "starters": [{"id": p["id"], "name": p["name"]} for p in side["players"][:11]],
                           "subs": [{"id": side["players"][11]["id"]}, {"id": unused}]}

        def both(key, fn):
            return {"key": key, "stats": [fn(h), fn(a)], "type": "text"}
        groups = [
            {"key": "top_stats", "stats": [
                {"key": "BallPossesion", "stats": [55, 45], "type": "graph"},
                both("expected_goals", lambda s: f"{s['xg']:.2f}"),
                both("total_shots", lambda s: len(s["shots"])),
                both("ShotsOnTarget", lambda s: sum(x["isOnTarget"] for x in s["shots"])),
                both("big_chance", lambda s: 2), both("corners", lambda s: 5),
                both("big_chance_missed_title", lambda s: 1),
                both("keeper_saves", lambda s: 3),
                {"key": "accurate_passes", "stats": ["438 (87%)", "347 (85%)"]}]},
            {"key": "expected_goals", "stats": [
                {"key": "expected_goals", "stats": [None, None], "type": "title"},
                both("expected_goals_non_penalty", lambda s: f"{s['xg']:.2f}"),
                both("expected_goals_open_play", lambda s: f"{s['xg'] * 0.8:.2f}"),
                both("expected_goals_set_play", lambda s: f"{s['xg'] * 0.2:.2f}"),
                both("expected_goals_on_target", lambda s: f"{s['xg']:.2f}")]}]
        payload = {
            "general": {"matchId": str(m["id"]), "leagueId": config.FOTMOB_IDS[m["code"]],
                        "matchRound": str(m["round"]), "coverageLevel": "xG",
                        "homeTeam": {"name": h["name"], "id": h["tid"]},
                        "awayTeam": {"name": a["name"], "id": a["tid"]},
                        "matchTimeUTCDate": m["kickoff"].strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        "started": True, "finished": True, "teamColors": {}},
            "header": {"teams": [{"name": h["name"], "id": h["tid"], "score": h["goals"]},
                                 {"name": a["name"], "id": a["tid"], "score": a["goals"]}],
                       "status": {"finished": True, "started": True, "cancelled": False,
                                  "scoreStr": f"{h['goals']} - {a['goals']}"}},
            "content": {"stats": {"Periods": {"All": {"stats": groups}, "FirstHalf": {"stats": []}}},
                        "playerStats": player_stats, "shotmap": {"shots": h["shots"] + a["shots"]},
                        "lineup": lineup, "matchFacts": {"events": {"events": events}, "poll": {"votes": 123}},
                        "h2h": {"matches": []}, "momentum": {"main": {}}},
            "seo": {"path": "x"}}
        return self.tweak.get(("match", match_id), lambda x: x)(payload)

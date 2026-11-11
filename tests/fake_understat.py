"""A small simulated Understat: same JSON shapes as the real site, deterministic content.

10 teams per league, double round robin (18 rounds, one per week from 15 August), 12 players
per team. Scores, shots and season totals are consistent with each other, like the real data.
"""

import random
from datetime import datetime, timedelta

from football import config
from football.understat import BudgetSpent, UnderstatError

TEAMS_PER_LEAGUE = 10
CODES = list(config.LEAGUES)


def round_robin(n):
    teams, rounds = list(range(n)), []
    for _ in range(n - 1):
        rounds.append([(teams[i], teams[n - 1 - i]) for i in range(n // 2)])
        teams = [teams[0]] + [teams[-1]] + teams[1:-1]
    return rounds + [[(b, a) for a, b in r] for r in rounds]


class FakeUnderstat:
    def __init__(self, now, max_requests=100000):
        self.now, self.max_requests, self.used = now, max_requests, 0
        self.fail_leagues, self.fail_matches, self.tweak = set(), set(), {}
        self.calls = []
        self._matches = {}                      # match_id -> simulated match

    # ------------------------------------------------------------ simulation
    def _team(self, code, i):
        base = CODES.index(code) * 100 + i + 1
        return base, f"{code} Team {i + 1:02d}"

    def _season(self, code, season):
        out = []
        for r, pairs in enumerate(round_robin(TEAMS_PER_LEAGUE)):
            kickoff = datetime(season, 8, 15, 15, 0) + timedelta(days=7 * r)
            for k, (h, a) in enumerate(pairs):
                mid = CODES.index(code) * 1_000_000 + season * 1000 + r * 10 + k
                out.append(self._match(mid, code, season, kickoff, h, a))
        return out

    def _match(self, mid, code, season, kickoff, h, a):
        if mid in self._matches:
            return self._matches[mid]
        rnd = random.Random(mid)
        sides = {}
        for side, t in (("h", h), ("a", a)):
            tid, title = self._team(code, t)
            strength = 0.8 + 0.12 * (TEAMS_PER_LEAGUE - t)          # team 01 is the strongest
            players, shots = [], []
            for n in range(12):
                players.append({"id": str(len(players) + 1 + tid * 100 + mid * 100000),
                                "player_id": str(tid * 100 + n), "team_id": str(tid),
                                "player": f"Player {tid}-{n:02d}", "h_a": side,
                                "position": "Sub" if n == 11 else "FW" if n >= 9 else "MC",
                                "time": "20" if n == 11 else "90" if n != 10 else "70",
                                "goals": 0, "own_goals": "0", "shots": 0, "xG": 0.0, "assists": "0",
                                "xA": "0.1", "key_passes": "1", "yellow_card": "0", "red_card": "0",
                                "xGChain": "0.2", "xGBuildup": "0.1"})
            for s in range(rnd.randint(6, 14)):
                p = players[rnd.choice([8, 9, 9, 10, 10, 10, 11])]
                xg = round(rnd.uniform(0.03, 0.35) * strength, 3)
                goal = rnd.random() < xg * 1.1
                shots.append({"id": str(mid * 100 + len(shots) + (0 if side == "h" else 50)),
                              "minute": str(rnd.randint(1, 90)), "result": "Goal" if goal else
                              rnd.choice(["SavedShot", "MissedShots", "BlockedShot"]),
                              "X": "0.9", "Y": "0.5", "xG": str(xg), "player": p["player"],
                              "h_a": side, "player_id": p["player_id"], "situation": "OpenPlay",
                              "shotType": "RightFoot", "lastAction": "Pass", "player_assisted": None})
                p["shots"] += 1
                p["xG"] = round(p["xG"] + xg, 3)
                p["goals"] += int(goal)
            sides[side] = {"tid": tid, "title": title, "players": players, "shots": shots,
                           "goals": sum(p["goals"] for p in players),
                           "xg": round(sum(float(s["xG"]) for s in shots), 3)}
        m = {"id": mid, "code": code, "season": season, "kickoff": kickoff, **sides}
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
            raise UnderstatError(f"getLeagueData/{code}/{season}: HTTP 503")
        if season > config.current_season(self.now.date()):
            return None
        dates, teams, players = [], {}, {}
        for m in self._season(code, season):
            played = m["kickoff"] + timedelta(hours=2) <= self.now
            h, a = m["h"], m["a"]
            dates.append({
                "id": str(m["id"]), "isResult": played, "datetime": m["kickoff"].strftime("%Y-%m-%d %H:%M:%S"),
                "h": {"id": str(h["tid"]), "title": h["title"], "short_title": h["title"][:3]},
                "a": {"id": str(a["tid"]), "title": a["title"], "short_title": a["title"][:3]},
                "goals": {"h": str(h["goals"]) if played else None, "a": str(a["goals"]) if played else None},
                "xG": {"h": str(h["xg"]) if played else None, "a": str(a["xg"]) if played else None},
                "forecast": {"w": "0.5", "d": "0.3", "l": "0.2"}})
            if not played:
                continue
            for side, me, opp in (("h", h, a), ("a", a, h)):
                t = teams.setdefault(str(me["tid"]), {"id": str(me["tid"]), "title": me["title"], "history": []})
                gf, ga = me["goals"], opp["goals"]
                pts = 3 if gf > ga else 1 if gf == ga else 0
                t["history"].append({
                    "h_a": side, "xG": me["xg"], "xGA": opp["xg"], "npxG": me["xg"], "npxGA": opp["xg"],
                    "ppda": {"att": 200, "def": 20}, "ppda_allowed": {"att": 180, "def": 20},
                    "deep": 5, "deep_allowed": 4, "scored": gf, "missed": ga,
                    "xpts": round(1.4 + (me["xg"] - opp["xg"]) / 2, 3),
                    "result": "w" if pts == 3 else "d" if pts == 1 else "l",
                    "date": m["kickoff"].strftime("%Y-%m-%d %H:%M:%S"), "pts": pts})
                for p in me["players"]:
                    s = players.setdefault(p["player_id"], {
                        "id": p["player_id"], "player_name": p["player"], "games": 0, "time": 0,
                        "goals": 0, "xG": 0.0, "assists": 0, "xA": 0.0, "shots": 0, "key_passes": 0,
                        "yellow_cards": 0, "red_cards": 0, "position": "F", "team_title": me["title"],
                        "npg": 0, "npxG": 0.0, "xGChain": 0.0, "xGBuildup": 0.0})
                    s["games"] += 1
                    s["time"] += int(p["time"])
                    s["goals"] += p["goals"]
                    s["npg"] += p["goals"]
                    s["shots"] += p["shots"]
                    s["xG"] = round(s["xG"] + p["xG"], 3)
                    s["npxG"] = s["xG"]
        payload = {"dates": dates, "teams": teams,
                   "players": [dict(p, **{k: str(p[k]) for k in ("games", "time", "goals", "xG", "shots", "npg", "npxG")})
                               for p in players.values()]}
        return self.tweak.get(("league", code, season), lambda x: x)(payload)

    def match(self, match_id):
        self._count()
        self.calls.append(("match", match_id))
        if match_id in self.fail_matches:
            raise UnderstatError(f"getMatchData/{match_id}: HTTP 503")
        if match_id not in self._matches:                  # ids encode league and season
            idx, season = match_id // 1_000_000, match_id % 1_000_000 // 1000
            if idx < len(CODES):
                self._season(CODES[idx], season)
        m = self._matches.get(match_id)
        if m is None:
            return None
        payload = {"rosters": {s: {p["id"]: dict(p, goals=str(p["goals"]), shots=str(p["shots"]),
                                                 xG=str(p["xG"])) for p in m[s]["players"]}
                               for s in ("h", "a")},
                   "shots": {s: [dict(x) for x in m[s]["shots"]] for s in ("h", "a")}}
        return self.tweak.get(("match", match_id), lambda x: x)(payload)

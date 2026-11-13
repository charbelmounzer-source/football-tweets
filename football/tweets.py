"""Find interesting, matchday-related stats and draft tweets about them.

How it works
  1. Context: the last matchday (results within 4 days of the latest one, if that is less
     than 10 days old) and the fixtures of the next 7 days.
  2. Fact patterns, each run over every team / player / stat (no hand-written facts):
       contrast   top 3 for one stat but bottom 5 for its partner (shots vs conversion...)
       streak     first clean sheet / win / away win in N matches; unbeaten, winning,
                  winless runs - compared with the team's and the league's longest
       start      points, goals, conceded, xG after N matches vs the same point of past seasons
       overtake   a scorer passes or matches his tally for a previous club
       luck       goals vs xG, points vs expected points (FotMob's season figures),
                  finishing (non-penalty goals vs npxG)
       preview    head-to-head runs for the next fixtures
       player_streak  a player scoring (or involved in a goal) in N appearances in a row
       pace       a player's goals / assists after N team matches vs every player-season
                  since the first season loaded
       performance  last matchday's standouts: hat-tricks, 3+ assists, goalkeeping (xGOT)
       leader     season leaders with a clear gap: chances created, big chances, dribbles,
                  tackles, goals prevented; teams: set-piece goals, big chances missed
  3. Scoring: rarity x context (last matchday / next fixture) x club popularity, minus
     facts already drafted in the last 14 days. At most MAX_SHARE_PER_KIND of the drafts
     come from one pattern, so one kind of fact cannot fill the list.
  4. Templates write the tweet. Every number comes from the data; the evidence is stored
     next to each draft.

Reads the warehouse (core.*), writes tweets.drafts. The drafts table is also the history
that prevents repeats. History starts with the first season loaded for each league, so
facts say "since 2020/21", never "ever".
"""

import csv
import hashlib
import json
import math
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from .config import season_label

CLUBS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "config", "clubs.csv")
RECENT_DAYS = 4          # a "matchday" = results within this many days of the latest one
STALE_DAYS = 10          # ...and the latest one must be this recent, or there is no matchday
UPCOMING_DAYS = 7
REPEAT_DAYS = 14         # don't draft a fact again if it was drafted in this window
MAX_SHARE_PER_KIND = 0.3 # at most this share of the drafts from one fact pattern
TWEET_LIMIT = 280
DT = "%Y-%m-%d %H:%M:%S"

# Punch lines, one set per meaning, so the line always fits the stat it follows
# ("Wasteful" only where chances were wasted, "Hard to beat" only on an unbeaten run...).
PUNCH = {
    "unbeaten": ["Hard to beat. 🧱", "Still standing. 💪", "Nobody has found the answer. 🛡️"],
    "winning": ["Flying. 🚀", "Relentless. 🔥", "Can anyone stop them? 👀"],
    "scoring": ["Always a threat. ⚽", "Goals every week. 🎯", "They always find a way. 🔥"],
    "winless": ["Something has to change. 👀", "Searching for answers. 🤔", "Not the numbers you want. 📉"],
    "losing": ["Crisis? 🚨", "Something has to change. 👀", "Tough times. 😬"],
    "best_points": ["Flying. 🚀", "Statement. 💪", "Impressive. 👏"],
    "best_goals": ["Goals galore. ⚽", "Firing. 🔥", "Impressive. 👏"],
    "best_xg": ["Creating chances for fun. 🎨", "Carving teams open. 🔪", "Impressive. 👏"],
    "best_conceded": ["Solid at the back. 🧱", "Tough to break down. 🛡️", "Impressive. 👏"],
    "worst_points": ["Nowhere near good enough. 🤷‍♂️", "Something has to change. 👀", "Not the numbers you want. 📉"],
    "worst_goals": ["Where are the goals? 🤷‍♂️", "Goals are hard to come by. 😬", "Not the numbers you want. 📉"],
    "worst_xg": ["Not creating enough. 📉", "Where are the chances? 🤷‍♂️", "Something has to change. 👀"],
    "worst_conceded": ["Leaking. 😬", "Something has to change at the back. 👀", "Not the numbers you want. 📉"],
    "wasteful": ["Wasteful. 😬", "Need to be more clinical. 🎯", "So many chances, so few goals. 🤦‍♂️"],
    "clinical": ["Clinical. 🎯", "Making every shot count. 🎯", "Ruthless. 🔪"],
    "lucky": ["Living on the edge. 😅", "Riding their luck? 🍀", "Can it last? 🤔"],
    "unlucky": ["Deserved more. 😤", "Football can be cruel. 💔", "The numbers say better times are coming. 📈"],
    "keeper": ["Brick wall. 🧱", "What a keeper. 🧤", "Saving his team. 🙌"],
    "finisher": ["Clinical. 🎯", "Ice cold. 🥶", "Lethal. 🔥"],
    "fire": ["On fire 🔥", "Unstoppable 🔥", "What a run 🔥"],
}


# ------------------------------------------------------------------ small helpers
def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def pick(options, key):
    """Deterministic choice, so a fact always gets the same wording."""
    return options[int(hashlib.md5(key.encode()).hexdigest(), 16) % len(options)]


def fmt(v, kind):
    if kind == "int":
        return f"{int(round(v))}"
    if kind == "pct":
        return f"{v:.1f}%"
    return f"{v:.1f}"


def nice_date(iso):
    d = date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%B %Y')}"


def tweet_length(text):
    """Length as X counts it: most emoji and non-Latin characters weigh 2."""
    n = 0
    for ch in text:
        cp = ord(ch)
        light = cp <= 4351 or 8192 <= cp <= 8205 or 8208 <= cp <= 8223 or 8242 <= cp <= 8247
        n += 1 if light else 2
    return n


def ranks(values, higher_better):
    """Competition ranking (1 = best) with tie flags: {key: (rank, tied)}."""
    items = sorted(values.items(), key=lambda kv: -kv[1] if higher_better else kv[1])
    out, prev, prev_rank = {}, None, 0
    for i, (k, v) in enumerate(items, 1):
        r = prev_rank if prev is not None and abs(v - prev) < 1e-9 else i
        out[k], prev, prev_rank = r, v, r
    counts = defaultdict(int)
    for r in out.values():
        counts[r] += 1
    return {k: (r, counts[r] > 1) for k, r in out.items()}


def rank_phrase(rank, tied, n, best_word, worst_word, noun):
    """'the 2nd-most shots', 'the joint-worst conversion rate'."""
    if rank <= math.ceil(n / 2):
        k, word = rank, best_word
    else:
        k, word = n - rank + 1, worst_word
    prefix = ("joint-" if tied else "") + ("" if k == 1 else f"{ordinal(k)}-")
    return f"the {prefix}{word} {noun}"


def the(league):
    """'the Premier League', 'the Bundesliga', but 'La Liga', 'Serie A', 'Ligue 1'."""
    return f"the {league}" if league in ("Premier League", "Bundesliga") else league


def first_name(name):
    return (name or "").split()[0] if name else name


# ------------------------------------------------------------------ data
PM_COLS = ["match_id", "league", "season", "date", "kickoff_utc", "player_id", "player", "team_id",
           "team", "minutes", "goals", "assists", "shots", "penalty_goals", "npxg"]
PM_EXTRA = ["started", "xa", "key_passes", "big_chances_created", "dribbles", "tackles",
            "interceptions", "saves", "goals_conceded", "xgot_faced", "goals_prevented", "rating"]
SETTLE_DAYS = 2          # a match this recent may not have its details yet: not counted as missing


class Data:
    def __init__(self, wh, leagues, today):
        self.wh, self.today = wh, today
        cutoff = today.isoformat()
        tm = [r for r in wh.read("core.team_matches") if (r["date"] or "9") <= cutoff]
        tm.sort(key=lambda r: (r["kickoff_utc"] or "", r["match_id"]))
        if not tm:
            raise SystemExit("No team match data in the warehouse. Run the update first.")
        # results come with the league data; xG and shots only once a match's details are loaded
        ts_cols = ("xg", "xga", "shots", "shot_goals", "xgot", "big_chances_missed")
        stats = {(r["match_id"], r["team_id"]): r for r in wh.read(
            "core.team_stats", ["match_id", "team_id", *ts_cols])}
        for r in tm:
            st = stats.get((r["match_id"], r["team_id"]))
            opp = stats.get((r["match_id"], r["opponent_id"]))
            r["detailed"] = st is not None
            for k in ts_cols:
                r[k] = st[k] if st else None
            r["xgot_against"] = opp["xgot"] if opp else None
        # matches FotMob has no details for (known, never asked again) and their teams: a
        # player's tally for one of those teams in that season may be short of a goal or two
        raw = wh.read("raw.match", ["match_id", "problem"])
        unusable = {r["match_id"] for r in raw if r["problem"]}
        downloaded = {r["match_id"] for r in raw}
        self.holes = {(r["team_id"], r["season"]) for r in tm if r["match_id"] in unusable}
        self.tm = tm
        self.season = max(r["season"] for r in tm)
        all_leagues = sorted({r["league"] for r in tm})
        self.leagues = all_leagues if leagues in (None, "all") else leagues
        bad = [lg for lg in self.leagues if lg not in all_leagues]
        if bad:
            raise SystemExit(f"Unknown league(s): {', '.join(bad)}. Available: {', '.join(all_leagues)}")
        self.first_season = {}                       # league -> first season in the data
        self.team_seq = defaultdict(list)            # (league, team_id) -> rows, oldest first
        self.team_all = defaultdict(list)            # team_id -> rows in every league
        settled = (today - timedelta(days=SETTLE_DAYS)).isoformat()
        missing = defaultdict(int)                   # (league, season) -> matches without details
        for r in tm:
            self.first_season[r["league"]] = min(self.first_season.get(r["league"], 9999), r["season"])
            self.team_seq[(r["league"], r["team_id"])].append(r)
            self.team_all[r["team_id"]].append(r)
            if r["match_id"] not in downloaded and (r["date"] or "") <= settled:
                missing[(r["league"], r["season"])] += 1
        # player facts need every match downloaded: a league-season is complete when none is
        # still waiting for its details (the few FotMob cannot give are in self.holes)
        self.complete = {lg: missing.get((lg, self.season), 0) == 0 for lg in all_leagues}
        self.history_missing = sum(missing.values()) // 2        # two team rows per match
        self.history_complete = self.history_missing == 0

        self.standings = {(r["league"], r["team_id"]): r
                          for r in wh.read("core.standings", None, {"season": self.season})}
        self.pm = [p for p in wh.read("core.player_matches", PM_COLS + PM_EXTRA, {"season": self.season})
                   if (p["date"] or "9") <= cutoff]
        self.pm.sort(key=lambda p: (p["kickoff_utc"] or "", p["match_id"]))
        self.ps = self.player_seasons()
        self.fixtures = sorted(wh.read("core.fixtures", None, {"status": "upcoming"}),
                               key=lambda f: f["kickoff_utc"] or "")

        # context per league: last matchday's matches, next fixtures
        self.recent_matches, self.recent_teams, self.upcoming = {}, {}, {}
        stale = (today - timedelta(days=STALE_DAYS)).isoformat()
        end = (today + timedelta(days=UPCOMING_DAYS)).isoformat()
        for lg in self.leagues:
            played = [r for r in tm if r["league"] == lg and r["season"] == self.season]
            last = max((r["date"] for r in played), default=None)
            self.recent_matches[lg], self.recent_teams[lg] = set(), set()
            if last and last >= stale:               # no "matchday" in the summer break
                start = (date.fromisoformat(last) - timedelta(days=RECENT_DAYS - 1)).isoformat()
                rec = [r for r in played if r["date"] >= start]
                self.recent_matches[lg] = {r["match_id"] for r in rec}
                self.recent_teams[lg] = {r["team_id"] for r in rec}
            self.upcoming[lg] = [f for f in self.fixtures if f["league"] == lg
                                 and cutoff <= (f["date"] or "") <= end]

        self.clubs = {}
        if os.path.exists(CLUBS_FILE):
            with open(CLUBS_FILE, encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("club"):
                        self.clubs[r["club"].strip()] = {
                            "hashtag": (r.get("hashtag") or "").strip(),
                            "popularity": float(r.get("popularity") or 1)}
        self._history = {}
        self._all_pm = None
        self.unreliable = 0                          # scorers left out: history not complete

    def season_shots(self):
        """Shots of the current season (for the set-piece facts)."""
        if not hasattr(self, "_shots"):
            self._shots = self.wh.read("core.shots", ["match_id", "league", "team_id", "result",
                                                      "own_goal", "situation"], {"season": self.season})
        return self._shots

    def opponent(self, match_id, team_id):
        if not hasattr(self, "_opp"):
            self._opp = {(r["match_id"], r["team_id"]): r["opponent"] for r in self.tm
                         if r["season"] == self.season}
        return self._opp.get((match_id, team_id), "")

    def hole(self, team_id, season=None):
        return (team_id, self.season if season is None else season) in self.holes

    def all_player_matches(self):
        """Goals and assists of every player in every loaded season (for the pace facts)."""
        if self._all_pm is None:
            self._all_pm = [p for p in self.wh.read(
                "core.player_matches", ["match_id", "league", "season", "date", "kickoff_utc",
                                        "player_id", "player", "team_id", "goals", "assists"])
                if (p["date"] or "9") <= self.today.isoformat()]
        return self._all_pm

    def player_seasons(self):
        """Season totals per player and league, added up from his match rows - only for
        leagues whose current season is complete (otherwise the totals would be too low)."""
        out = {}
        for p in self.pm:
            if not self.complete.get(p["league"]):
                continue
            s = out.setdefault((p["league"], p["player_id"]), {
                "player_id": p["player_id"], "league": p["league"], "games": 0, "minutes": 0,
                "goals": 0, "penalty_goals": 0, "assists": 0, "shots": 0, "npxg": 0.0,
                "hole": False, "missing": set()})
            s["player"], s["team"], s["team_id"] = p["player"], p["team"], p["team_id"]   # latest
            s["games"] += 1
            s["hole"] |= self.hole(p["team_id"])          # a match of his team has no details
            for k in ("minutes", "goals", "penalty_goals", "assists", "shots", "npxg"):
                s[k] += p[k] or 0
            for k in PM_EXTRA:
                if k in ("started", "rating"):
                    continue
                if k in ("xgot_faced", "goals_prevented") and p.get("saves") is not None:
                    s[k] = s.get(k, 0) + (p[k] or 0)      # a keeper who faced no shot on target
                elif p.get(k) is None:
                    s["missing"].add(k)                   # not loaded yet (rebuild) or not a keeper
                else:
                    s[k] = s.get(k, 0) + p[k]
        for s in out.values():
            s["non_penalty_goals"] = s["goals"] - s["penalty_goals"]
        return list(out.values())

    def since(self, lg):
        return season_label(self.first_season[lg])

    def hashtag(self, team):
        return self.clubs.get(team, {}).get("hashtag", "")

    def popularity(self, team):
        return self.clubs.get(team, {}).get("popularity", 1.0)

    def upcoming_teams(self, lg):
        return {f["home_team_id"] for f in self.upcoming[lg]} | {f["away_team_id"] for f in self.upcoming[lg]}

    def context(self, lg, team_id):
        """1.5 if the team played on the last matchday, 1.3 if it plays in the next days."""
        if team_id in self.recent_teams.get(lg, set()):
            return 1.5
        if team_id in self.upcoming_teams(lg):
            return 1.3
        return 1.0

    def load_history(self, player_ids):
        """Every loaded match of these players, in every season (for the overtake facts)."""
        ids = sorted(set(player_ids))
        self._history = defaultdict(list)
        if ids and self.history_complete:
            for p in self.wh.read("core.player_matches", PM_COLS, {"player_id": ids}):
                if (p["date"] or "9") <= self.today.isoformat():
                    self._history[p["player_id"]].append(p)

    def club_tallies(self, pid):
        """{club: goals, assists, pens, games, first_season, league} over the loaded history,
        or None while some match of the loaded seasons still lacks its details: a tally with
        holes in it would be wrong, so none is given."""
        if not self.history_complete:
            return None
        clubs = {}
        for r in self._history.get(pid, []):
            c = clubs.setdefault(r["team"], {"goals": 0, "assists": 0, "pens": 0, "games": 0,
                                             "first_season": r["season"], "league": r["league"],
                                             "hole": False})
            c["hole"] |= self.hole(r["team_id"], r["season"])
            c["goals"] += r["goals"] or 0
            c["assists"] += r["assists"] or 0
            c["pens"] += r["penalty_goals"] or 0
            c["games"] += 1
            if r["season"] < c["first_season"]:
                c["first_season"], c["league"] = r["season"], r["league"]
        return clubs


class Fact:
    def __init__(self, kind, league, entity, text, rarity, evidence, fact_id, team=None,
                 team_id=None, context=1.0):
        self.kind, self.league, self.entity, self.text = kind, league, entity, text
        self.rarity, self.evidence, self.fact_id = rarity, evidence, fact_id
        self.team, self.team_id, self.context = team, team_id, context
        self.score = 0.0


# ------------------------------------------------------------------ 1. contrast
CONTRAST_METRICS = {   # key: (higher is better, best word, worst word, noun, format)
    "shots": (True, "most", "fewest", "shots", "int"),
    "conversion": (True, "best", "worst", "conversion rate", "pct"),
    "goals": (True, "most", "fewest", "goals", "int"),
    "xg": (True, "highest", "lowest", "xG", "1"),
    "conceded": (False, "fewest", "most", "goals conceded", "int"),
    "xga": (False, "lowest", "highest", "xG against", "1"),
    "points": (True, "most", "fewest", "points", "int"),
    "xpts": (True, "most", "fewest", "expected points", "1"),
}
# (process stat, outcome stat): when the outcome lags the process -> "bad"/"unlucky",
# when the outcome beats the process -> "good"/"lucky"
CONTRAST_PAIRS = [("shots", "conversion", "wasteful", "clinical"), ("xg", "goals", "wasteful", "clinical"),
                  ("xga", "conceded", "unlucky", "lucky"), ("xpts", "points", "unlucky", "lucky")]


def team_season_stats(d, lg):
    """Current-season totals per team. Goals and points are added up from the results; xG,
    xG against and expected points are FotMob's own season figures (league table), used only
    when the table counts the same number of matches; shots need every match's details."""
    stats, names, rows_of = defaultdict(lambda: defaultdict(float)), {}, defaultdict(list)
    for (league, tid), rows in d.team_seq.items():
        if league != lg:
            continue
        for r in rows:
            if r["season"] != d.season:
                continue
            s = stats[tid]
            names[tid] = r["team"]
            rows_of[tid].append(r)
            s["games"] += 1
            s["goals"] += r["goals_for"]
            s["conceded"] += r["goals_against"]
            s["points"] += r["points"]
    for tid, s in stats.items():
        table = d.standings.get((lg, tid))
        if table and table["played"] == s["games"] and not table["deduction"]:
            for key in ("xg", "xga", "xpts"):
                if table[key] is not None:
                    s[key] = table[key]
        rows = rows_of[tid]
        if all(r["xgot_against"] is not None for r in rows):
            s["xgot_against"] = sum(r["xgot_against"] for r in rows)
        if all(r["big_chances_missed"] is not None for r in rows):
            s["big_chances_missed"] = sum(r["big_chances_missed"] for r in rows)
        if all(r["shots"] is not None and r["shot_goals"] is not None for r in rows):
            shots = sum(r["shots"] for r in rows)
            if shots:
                s["shots"] = shots
                s["conversion"] = 100 * sum(r["shot_goals"] for r in rows) / shots
    return stats, names


def season_under_way(stats):
    n = len(stats)
    return n >= 10 and sorted(s["games"] for s in stats.values())[n // 2] >= 5


def gen_contrast(d, lg):
    stats, names = team_season_stats(d, lg)
    n = len(stats)
    if not season_under_way(stats):
        return []
    rk = {}
    for m, (hib, *_r) in CONTRAST_METRICS.items():
        vals = {t: s[m] for t, s in stats.items() if m in s}
        if len(vals) == n:
            rk[m] = ranks(vals, hib)
    facts = []
    for a, b, lag, beat in CONTRAST_PAIRS:
        if a not in rk or b not in rk:
            continue
        for tid in stats:
            (ra, ta), (rb, tb) = rk[a][tid], rk[b][tid]
            if ra <= 3 and rb >= n - 4:
                mood = lag
            elif rb <= 3 and ra >= n - 4:
                mood = beat
            else:
                continue
            ma, mb = CONTRAST_METRICS[a], CONTRAST_METRICS[b]
            va, vb = stats[tid][a], stats[tid][b]
            team = names[tid]
            pa = rank_phrase(ra, ta, n, ma[1], ma[2], ma[3])
            pb = rank_phrase(rb, tb, n, mb[1], mb[2], mb[3])
            text = (f"{team} have {pa} in {the(lg)} ({fmt(va, ma[4])}) "
                    f"but {pb} ({fmt(vb, mb[4])}).\n\n{pick(PUNCH[mood], team + a + b)}")
            facts.append(Fact(
                "contrast", lg, team, text, rarity=4 + 4 * abs(ra - rb) / (n - 1),
                evidence={a: fmt(va, ma[4]), f"{a}_rank": f"{ra}/{n}", b: fmt(vb, mb[4]),
                          f"{b}_rank": f"{rb}/{n}", "games": int(stats[tid]["games"])},
                fact_id=f"contrast|{lg}|{a}|{b}|{tid}", team=team, team_id=tid,
                context=d.context(lg, tid)))
    return facts


# ------------------------------------------------------------------ 2. streaks
DROUGHT_EVENTS = {   # event: (test, words, minimum matches without it, venue counted)
    "clean_sheet": (lambda r: r["goals_against"] == 0, "kept a clean sheet", 7, None),
    "win": (lambda r: r["result"] == "W", "won a match", 6, None),
    "away_win": (lambda r: r["result"] == "W", "won away from home", 6, "away"),
    "home_win": (lambda r: r["result"] == "W", "won at home", 5, "home"),
    "scored": (lambda r: (r["goals_for"] or 0) > 0, "scored", 4, None),
}
RUNS = {   # run: (test, words, minimum length, mood); the punch line is PUNCH[run]
    "unbeaten": (lambda r: r["result"] != "L", "unbeaten", 6, "good"),
    "winning": (lambda r: r["result"] == "W", "winning", 4, "good"),
    "winless": (lambda r: r["result"] != "W", "winless", 6, "bad"),
    "losing": (lambda r: r["result"] == "L", "losing", 4, "bad"),
    "scoring": (lambda r: (r["goals_for"] or 0) > 0, "scoring", 8, "good"),
}


def team_sequences(d, lg):
    return {tid: rows for (league, tid), rows in d.team_seq.items() if league == lg}


def run_length(rows, test):
    n = 0
    for r in reversed(rows):
        if not test(r):
            break
        n += 1
    return n


def longest_run(rows, test):
    best = cur = 0
    for r in rows:
        cur = cur + 1 if test(r) else 0
        best = max(best, cur)
    return best


def gen_streaks(d, lg):
    seq = team_sequences(d, lg)
    since = d.since(lg)
    facts = []
    for tid, rows in seq.items():
        last = rows[-1]
        if last["match_id"] not in d.recent_matches.get(lg, set()):
            continue
        team, opp = last["team"], last["opponent"]
        # droughts that just ended
        for ev, (test, words, minimum, venue) in DROUGHT_EVENTS.items():
            if not test(last) or (venue and last["home_away"] != venue):
                continue
            ev_rows = [r for r in rows if not venue or r["home_away"] == venue]
            prev = [i for i, r in enumerate(ev_rows[:-1]) if test(r)]
            gap = len(ev_rows) - 1 - (prev[-1] + 1) if prev else len(ev_rows) - 1
            if gap < minimum:
                continue
            match_word = f"{venue} matches" if venue else "matches"
            if prev:
                prev_row = ev_rows[prev[-1]]
                days = (date.fromisoformat(last["date"]) - date.fromisoformat(prev_row["date"])).days
                months = (f" ({round(days / 30.4)} months)"
                          if days >= 60 and last["season"] - prev_row["season"] <= 1 else "")
                text = (f"🚨 {team} have {words} in {the(lg)} for the first time in {gap} {match_word}{months}."
                        f"\n\nThe last time was against {prev_row['opponent']} on {nice_date(prev_row['date'])}.")
            elif all(r["season"] == d.season for r in rows):
                # a team new to the league (promoted): its history here is this season only
                text = (f"🚨 {team} have {words} in {the(lg)} for the first time this season, "
                        f"at the {ordinal(gap + 1)} attempt.")
            else:
                text = (f"🚨 {team} have {words} in {the(lg)} for the first time in our data "
                        f"(since {since}).")
            facts.append(Fact(
                "streak", lg, team, text, rarity=5 + min(gap, 30) / 4,
                evidence={"event": ev, "matches_without": gap, "ended_vs": opp, "date": last["date"],
                          "previous": ev_rows[prev[-1]]["date"] if prev else f"none since {since}",
                          "counted": match_word},
                fact_id=f"drought|{lg}|{ev}|{tid}|{last['match_id']}", team=team, team_id=tid,
                context=1.5))
        # ongoing runs
        for run, (test, words, minimum, mood) in RUNS.items():
            length = run_length(rows, test)
            if length < minimum or length == len(rows):
                continue                             # a run older than our data has no known length
            own_best = longest_run(rows[:-length], test) if len(rows) > length else 0
            league_best = max((longest_run(r2, test) for t2, r2 in seq.items() if t2 != tid), default=0)
            notes, rarity = [], 3 + min(length, 30) / 3
            seasons_here = len({r["season"] for r in rows})
            if length > own_best and seasons_here >= 3:
                # only with some history: "their longest" means little for a promoted team
                notes.append(f"their longest in any of their {seasons_here} {lg} seasons since {since}")
                rarity += 2
            if length > league_best:
                notes.append(f"the longest by any {lg} team since {since}")
                rarity += 3
            if not notes and length < minimum + 3:
                continue
            extra = f" - {' and '.join(notes)}" if notes else ""
            article = "an" if length in (8, 11, 18) or 80 <= length < 90 else "a"
            text = (f"📊 {team} are on {article} {length}-match {words} run in {the(lg)}{extra}."
                    f"\n\n{pick(PUNCH[run], team + run)}")
            facts.append(Fact(
                "streak", lg, team, text, rarity=rarity,
                evidence={"run": run, "length": length, "team_best_before": own_best,
                          "other_teams_best": league_best, "last_match": last["date"]},
                fact_id=f"run|{lg}|{run}|{tid}|{length}", team=team, team_id=tid, context=1.5))
    return facts


# ------------------------------------------------------------------ 3. season start
START_METRICS = {   # key: (column, higher is better, words); punch line PUNCH["best_"/"worst_" + key]
    "points": ("points", True, "points"),
    "goals": ("goals_for", True, "goals"),
    "conceded": ("goals_against", False, "goals conceded"),
    "xg": ("xg", True, "xG"),
}
START_MIN_PAST_SEASONS = 3   # "their best start" needs a few seasons to compare with


def gen_start(d, lg):
    seq = team_sequences(d, lg)
    since = d.since(lg)
    facts = []
    by_team_season = defaultdict(list)
    for tid, rows in seq.items():
        for r in rows:
            by_team_season[(tid, r["season"])].append(r)
    for tid in seq:
        cur = by_team_season.get((tid, d.season), [])
        n = len(cur)
        if n < 4 or cur[-1]["match_id"] not in d.recent_matches.get(lg, set()):
            continue
        team = cur[-1]["team"]
        best_fact = None
        for key, (col, hib, words) in START_METRICS.items():
            kind_fmt = "1" if key == "xg" else "int"
            total = lambda rs: sum((r[col] or 0) for r in rs[:n])
            # xG exists only for matches whose details are loaded: a season with a hole is left out
            usable = lambda rs: len(rs) >= n and all(r[col] is not None for r in rs[:n])
            if not usable(cur):
                continue
            now = total(cur)
            past = {s: total(rs) for (t, s), rs in by_team_season.items()
                    if t == tid and s != d.season and usable(rs)}
            if len(past) < START_MIN_PAST_SEASONS:
                continue
            better = (lambda a, b: a > b) if hib else (lambda a, b: a < b)
            if all(better(now, v) for v in past.values()):
                kind = "best"
            elif all(better(v, now) for v in past.values()):
                kind = "worst"
            else:
                continue
            league_all = {(t, s): total(rs) for (t, s), rs in by_team_season.items()
                          if s != d.season and usable(rs)}
            if key == "xg" and not d.history_complete:
                league_all = {}                           # no "best by any team" on a partial history
            league_record = bool(league_all) and all(better(now, v) for v in league_all.values())
            # the previous record: the best past value for a "best" start, the worst for a "worst"
            pick_ref = max if (kind == "best") == hib else min
            ref_v = pick_ref(past.values())
            ref_seasons = sorted(s for s, v in past.items() if v == ref_v)
            ref_when = season_label(ref_seasons[-1]) + (" and others" if len(ref_seasons) > 2 else
                                                         f" and {season_label(ref_seasons[0])}"
                                                         if len(ref_seasons) == 2 else "")
            v_txt, ref_txt = fmt(now, kind_fmt), fmt(ref_v, kind_fmt)
            if v_txt == ref_txt:
                continue                                  # equal once rounded: not a story
            if league_record and kind == "best":
                text = (f"📈 {team} have {v_txt} {words} after {n} {lg} matches - the best start by "
                        f"any {lg} team since {since}.")
                rarity = 9
            else:
                k = len(past) + 1
                text = (f"📊 {team} have {v_txt} {words} after {n} {lg} matches - their {kind} start in "
                        f"any of their {k} {lg} seasons since {since} (previous {kind}: "
                        f"{ref_txt} in {ref_when}).")
                rarity = 6 if kind == "best" else 6.5
            text += f"\n\n{pick(PUNCH[f'{kind}_{key}'], team + key)}"
            f = Fact("start", lg, team, text, rarity=rarity,
                     evidence={"stat": key, "now": v_txt, "matches": n, "kind": kind,
                               "past_seasons": "; ".join(f"{season_label(s)}: {fmt(v, kind_fmt)}"
                                                         for s, v in sorted(past.items())),
                               "league_record": league_record},
                     fact_id=f"start|{lg}|{key}|{kind}|{tid}|{n}", team=team, team_id=tid, context=1.5)
            if best_fact is None or f.rarity > best_fact.rarity:
                best_fact = f
        if best_fact:
            facts.append(best_fact)
    return facts


# ------------------------------------------------------------------ 4. overtake
def gen_overtake(d, lg):
    facts = []
    recent = d.recent_matches.get(lg, set())
    last_row = {}                                    # a player's latest match of the season
    for p in d.pm:
        if p["league"] == lg:
            last_row[p["player_id"]] = p
    scorers = [p for p in last_row.values() if p["match_id"] in recent and (p["goals"] or 0) > 0]
    opp_of = {(r["match_id"], r["team_id"]): r["opponent"] for r in d.tm if r["match_id"] in recent}
    for p in scorers:
        pid, team = p["player_id"], p["team"]
        clubs = d.club_tallies(pid)
        if not clubs or team not in clubs:
            d.unreliable += 1                        # history not complete yet
            continue
        here = clubs[team]
        if here["hole"]:
            continue                                 # a match of his club has no details
        before = here["goals"] - (p["goals"] or 0)
        for prev_team, c in clubs.items():
            if prev_team == team or c["goals"] < 5 or c["hole"]:
                continue
            if c["first_season"] <= d.first_season.get(c["league"], 0):
                continue                             # may have scored for them before our data starts
            if before < c["goals"] < here["goals"]:
                verb = "overtook"
            elif before < c["goals"] == here["goals"]:
                verb = "matched"
            else:
                continue
            opp = opp_of.get((p["match_id"], p["team_id"]), "")
            goal_word = "goal" if (p["goals"] or 0) == 1 else "goals"
            text = (f"⚽️ {p['player']}'s {goal_word} against {opp} meant he {verb} his top-flight "
                    f"league tally for {prev_team} ({c['goals']}).")
            if here["pens"] == 0 and c["pens"] > 0:
                text += (f"\n\n{first_name(p['player'])} has yet to score a penalty for {team} in the "
                         f"league, while {c['pens']} of his {c['goals']} for {prev_team} came from the spot.")
            ga = here["goals"] + here["assists"]
            text += f"\n\n{ga} G/A in {here['games']} games for {team}"
            text += f"… {pick(PUNCH['fire'], p['player'])}" if here["games"] and ga / here["games"] >= 0.5 else "."
            facts.append(Fact(
                "overtake", lg, p["player"], text, rarity=7 + min(c["goals"], 40) / 10,
                evidence={"goals_for_club": here["goals"], "before_match": before,
                          f"goals_for_{prev_team}": c["goals"], "previous_league": c["league"],
                          "pens_now": here["pens"], "pens_before": c["pens"],
                          "games_for_club": here["games"], "assists_for_club": here["assists"],
                          "note": f"top-5-league matches since {d.since(lg)}"},
                fact_id=f"overtake|{pid}|{prev_team}|{verb}", team=team, team_id=p["team_id"],
                context=1.5))
    return facts


# ------------------------------------------------------------------ 5. luck
def gen_luck(d, lg):
    stats, names = team_season_stats(d, lg)
    facts = []
    if season_under_way(stats):
        for metric, real, exp in (("goals", "goals", "xg"), ("points", "points", "xpts"),
                                  ("saves", "conceded", "xgot_against")):
            if any(exp not in s for s in stats.values()):
                continue                                  # FotMob's season figure missing for a team
            diffs = {t: s[real] - s[exp] for t, s in stats.items()}
            if metric == "saves":
                diffs = {t: -v for t, v in diffs.items()}    # conceding fewer than xGOT is good
            for tid, sign in ((max(diffs, key=diffs.get), 1), (min(diffs, key=diffs.get), -1)):
                dv, s, team = diffs[tid], stats[tid], names[tid]
                if abs(dv) < 3 or dv * sign <= 0:
                    continue
                if metric == "goals":
                    text = (f"{'📈' if sign > 0 else '📉'} {team} have scored {fmt(s['goals'], 'int')} {lg} "
                            f"goals from {fmt(s['xg'], '1')} xG ({dv:+.1f}) - the biggest "
                            f"{'over' if sign > 0 else 'under'}performance in the league this season.")
                elif metric == "saves":
                    if sign < 0:
                        continue                          # only the best shot-stopping
                    text = (f"🧤 {team} have conceded {fmt(s['conceded'], 'int')} {lg} goals from "
                            f"{fmt(s['xgot_against'], '1')} xGOT against (shots on target, weighted by "
                            f"quality) - the best shot-stopping in the league this season.")
                else:
                    text = (f"{'🍀' if sign > 0 else '💔'} {team} have {fmt(s['points'], 'int')} points but "
                            f"{fmt(s['xpts'], '1')} expected points ({dv:+.1f}) - the "
                            f"{'luckiest' if sign > 0 else 'unluckiest'} side in {the(lg)} on xPts.")
                mood = "keeper" if metric == "saves" else "lucky" if sign > 0 else "unlucky"
                text += f"\n\n{pick(PUNCH[mood], team + metric)}"
                facts.append(Fact(
                    "luck", lg, team, text, rarity=4 + min(abs(dv), 10) / 2,
                    evidence={real: fmt(s[real], "int"), exp: fmt(s[exp], "1"), "difference": f"{dv:+.1f}",
                              "games": int(s["games"])},
                    fact_id=f"luck|{lg}|{metric}|{'over' if sign > 0 else 'under'}|{tid}",
                    team=team, team_id=tid, context=d.context(lg, tid)))
    # players: finishing (non-penalty goals vs npxG), at least 15 shots
    players = [s for s in d.ps if s["league"] == lg and (s["shots"] or 0) >= 15
               and s["npxg"] is not None and not s["hole"]]
    if len(players) >= 10:
        diffs = {i: (s["non_penalty_goals"] or 0) - (s["npxg"] or 0) for i, s in enumerate(players)}
        top = max(diffs, key=diffs.get)
        s = players[top]
        if diffs[top] >= 2.5:
            team, tid = s["team"], s["team_id"]
            text = (f"🎯 {s['player']} has {s['non_penalty_goals']} non-penalty {lg} goals from "
                    f"{s['npxg']:.1f} npxG ({diffs[top]:+.1f}) - the best finishing in the league this "
                    f"season.\n\n{pick(PUNCH['finisher'], s['player'])}")
            facts.append(Fact(
                "luck", lg, s["player"], text, rarity=5 + min(diffs[top], 8) / 2,
                evidence={"non_penalty_goals": s["non_penalty_goals"], "npxg": f"{s['npxg']:.2f}",
                          "shots": s["shots"], "minutes": s["minutes"]},
                fact_id=f"finishing|{lg}|{s['player_id']}", team=team, team_id=tid,
                context=d.context(lg, tid)))
    return facts


# ------------------------------------------------------------------ 6. preview
def gen_preview(d, lg):
    facts = []
    for f in d.upcoming[lg]:
        h, a = f["home_team_id"], f["away_team_id"]
        day = datetime.strptime(f["kickoff_utc"], DT).strftime("%A")
        best = None
        for team, opp, tid, oid in ((f["home_team"], f["away_team"], h, a),
                                    (f["away_team"], f["home_team"], a, h)):
            rows = [r for r in d.team_all.get(tid, []) if r["opponent_id"] == oid]
            if len(rows) < 4:
                continue
            winless = run_length(rows, lambda r: r["result"] != "W")
            unbeaten = run_length(rows, lambda r: r["result"] != "L")
            wins = run_length(rows, lambda r: r["result"] == "W")
            if winless >= 4:
                dr = sum(1 for r in rows[-winless:] if r["result"] == "D")
                ls = winless - dr
                text = (f"👀 {team} have failed to beat {opp} in their last {winless} league meetings "
                        f"({dr} draw{'s' if dr != 1 else ''}, {ls} defeat{'s' if ls != 1 else ''}). "
                        f"They meet again on {day}.")
                rarity, kind = 4 + winless / 2, "winless"
            elif wins >= 3:
                text = (f"👀 {team} have won their last {wins} league meetings with {opp}. "
                        f"They meet again on {day}.")
                rarity, kind = 4 + wins / 2, "wins"
            elif unbeaten >= 5:
                text = (f"👀 {team} are unbeaten in their last {unbeaten} league meetings with {opp}. "
                        f"They meet again on {day}.")
                rarity, kind = 3.5 + unbeaten / 3, "unbeaten"
            else:
                continue
            last = rows[-1]
            fact = Fact(
                "preview", lg, f"{team} v {opp}", text, rarity=rarity,
                evidence={"run": kind, "meetings_considered": len(rows), "last_meeting": last["date"],
                          "last_score": f"{last['goals_for']}-{last['goals_against']}",
                          "fixture": f"{f['home_team']} v {f['away_team']} {f['kickoff_utc']} UTC"},
                fact_id=f"preview|{f['match_id']}|{tid}|{kind}", team=team, team_id=tid, context=1.3)
            # both sides of a fixture tell the same story: keep the stronger one
            weight = fact.rarity * (1 + 0.25 * (d.popularity(team) - 1))
            if best is None or weight > best[0]:
                best = (weight, fact)
        if best:
            facts.append(best[1])
    return facts


# ------------------------------------------------------------------ 7. player streaks
def player_rows(d, lg):
    """Current-season appearances per player in this league, oldest first."""
    out = defaultdict(list)
    for p in d.pm:
        if p["league"] == lg:
            out[p["player_id"]].append(p)
    return out


def gen_player_streaks(d, lg):
    if not d.complete.get(lg):
        return []
    rows_of = player_rows(d, lg)
    recent = d.recent_matches.get(lg, set())
    scored = lambda p: (p["goals"] or 0) > 0
    involved = lambda p: (p["goals"] or 0) + (p["assists"] or 0) > 0
    best = {"scoring": defaultdict(int), "involved": defaultdict(int)}
    for pid, rows in rows_of.items():
        best["scoring"][pid] = longest_run(rows, scored)
        best["involved"][pid] = longest_run(rows, involved)
    facts = []
    for pid, rows in rows_of.items():
        last = rows[-1]
        if last["match_id"] not in recent or d.hole(last["team_id"]):
            continue
        for run, test, minimum in (("scoring", scored, 4), ("involved", involved, 6)):
            n = run_length(rows, test)
            if n < minimum or (run == "involved" and run_length(rows, scored) >= 4):
                continue                              # the scoring run tells it better
            goals = sum(p["goals"] or 0 for p in rows[-n:])
            assists = sum(p["assists"] or 0 for p in rows[-n:])
            others = max((v for q, v in best[run].items() if q != pid), default=0)
            where = (f"in each of his {n} {lg} appearances this season" if n == len(rows)
                     else f"in {n} {lg} appearances in a row")
            if run == "scoring":
                text = f"🔥 {last['player']} has scored {where} ({goals} goals)"
            else:
                text = (f"🔥 {last['player']} has scored or assisted {where} "
                        f"({goals} goals, {assists} assists)")
            rarity = 5 + min(n, 15) / 1.5
            if n > others:
                text += f" - the longest run by any {lg} player this season"
                rarity += 2
            text += f".\n\n{pick(PUNCH['fire'], last['player'] + run)}"
            facts.append(Fact(
                "player_streak", lg, last["player"], text, rarity=rarity,
                evidence={"run": run, "appearances": n, "goals": goals, "assists": assists,
                          "season_appearances": len(rows), "longest_by_others": others,
                          "last_match": last["date"]},
                fact_id=f"player_run|{lg}|{run}|{pid}|{n}", team=last["team"],
                team_id=last["team_id"], context=1.5))
    return facts


# ------------------------------------------------------------------ 8. pace
PACE_METRICS = {   # key: (words, minimum)
    "goals": ("goals", 6),
    "assists": ("assists", 5),
    "involvements": ("goal involvements (goals + assists)", 9),
}


def gen_pace(d, lg):
    """A player's total after N matches of his team vs every player-season of this league
    since the first season loaded, at the same point."""
    if not (d.history_complete and d.complete.get(lg)):
        return []
    index = {}                                        # (match_id, team_id) -> team's Nth match
    for (league, tid), rows in d.team_seq.items():
        if league != lg:
            continue
        n_by_season = defaultdict(int)
        for r in rows:
            n_by_season[r["season"]] += 1
            index[(r["match_id"], tid)] = n_by_season[r["season"]]
    tallies = defaultdict(lambda: defaultdict(lambda: [0, 0]))   # (season, player, team) -> N -> [g, a]
    names = {}
    for p in d.all_player_matches():
        n = index.get((p["match_id"], p["team_id"])) if p["league"] == lg else None
        if n:
            tallies[(p["season"], p["player_id"], p["team_id"])][n] = [p["goals"] or 0, p["assists"] or 0]
            names[p["player_id"]] = p["player"]

    def at(key, n):
        g = sum(v[0] for k, v in tallies[key].items() if k <= n)
        a = sum(v[1] for k, v in tallies[key].items() if k <= n)
        return {"goals": g, "assists": a, "involvements": g + a}
    recent = d.recent_matches.get(lg, set())
    facts = []
    for p in d.pm:
        if p["league"] != lg or p["match_id"] not in recent or d.hole(p["team_id"]):
            continue
        if not (p["goals"] or p["assists"]):
            continue
        n = index.get((p["match_id"], p["team_id"]))
        if not n or n != len([r for r in d.team_seq[(lg, p["team_id"])] if r["season"] == d.season]):
            continue                                  # not his team's latest match
        me = at((d.season, p["player_id"], p["team_id"]), n)
        best_fact = None
        for metric, (words, minimum) in PACE_METRICS.items():
            v = me[metric]
            if v < minimum:
                continue
            rivals = []
            for key in tallies:
                if key == (d.season, p["player_id"], p["team_id"]):
                    continue
                season, pid, tid = key
                other = at(key, n)[metric]
                # a team-season with a match FotMob has no details for may hide a goal
                rivals.append((other + (1 if d.hole(tid, season) else 0), other, season, pid))
            margin, shown, *who = max(rivals, default=(0, 0, None, None))
            if v <= margin:
                continue
            top = (shown, *who)
            if top[1] is None:
                prev = ""
            elif top[1] == d.season:                  # a rival this season, not a past record
                prev = f" (next best: {names.get(top[2], '?')}, {top[0]})"
            elif top[2] == p["player_id"]:
                prev = f" (his own previous best: {top[0]} in {season_label(top[1])})"
            else:
                prev = f" (previous best: {top[0]}, {names.get(top[2], '?')} in {season_label(top[1])})"
            text = (f"📈 {p['player']} has {v} {lg} {words} after {n} matches of the season - the "
                    f"most by any {lg} player at this stage since {d.since(lg)}{prev}.")
            f = Fact("pace", lg, p["player"], text, rarity=9 + min(v - margin, 4) / 2,
                     evidence={"metric": metric, "value": v, "team_matches": n,
                               "previous_best": top[0],
                               "previous_best_by": f"{names.get(top[2], '?')} {season_label(top[1])}"
                               if top[1] is not None else None},
                     fact_id=f"pace|{lg}|{metric}|{p['player_id']}|{n}", team=p["team"],
                     team_id=p["team_id"], context=1.5)
            if best_fact is None or f.rarity > best_fact.rarity:
                best_fact = f
        if best_fact:
            facts.append(best_fact)
    return facts


# ------------------------------------------------------------------ 9. performances
def gen_performances(d, lg):
    recent = d.recent_matches.get(lg, set())
    season_rows = [p for p in d.pm if p["league"] == lg]
    facts = []
    keepers = [p for p in season_rows if p.get("goals_prevented") is not None]
    best_gp = max((p["goals_prevented"] for p in keepers), default=None)
    for p in season_rows:
        if p["match_id"] not in recent:
            continue
        opp = d.opponent(p["match_id"], p["team_id"])
        name, goals, assists = p["player"], p["goals"] or 0, p["assists"] or 0
        if goals >= 3:
            earlier = sorted((q for q in season_rows if (q["goals"] or 0) >= 3),
                             key=lambda q: (q["kickoff_utc"] or "", q["match_id"]))
            nth = [q["match_id"] for q in earlier].index(p["match_id"]) + 1
            what = "a hat-trick" if goals == 3 else f"{goals} goals"
            which = "the first" if nth == 1 else f"the {ordinal(nth)}"
            text = f"🎩 {name} scored {what} against {opp} - {which} {lg} hat-trick this season."
            if d.history_complete and not d.hole(p["team_id"]):
                mine = sum(1 for q in d.all_player_matches()
                           if q["player_id"] == p["player_id"] and (q["goals"] or 0) >= 3)
                if mine >= 2:
                    text += (f"\n\nHis {ordinal(mine)} in Europe's top five leagues since "
                             f"{season_label(min(d.first_season.values()))}.")
            facts.append(Fact("performance", lg, name, text, rarity=7.5 + goals - 3,
                              evidence={"goals": goals, "opponent": opp, "date": p["date"],
                                        "hat_tricks_in_league_this_season": len(earlier)},
                              fact_id=f"hat_trick|{p['match_id']}|{p['player_id']}", team=p["team"],
                              team_id=p["team_id"], context=1.5))
        if assists >= 3:
            n = sum(1 for q in season_rows if (q["assists"] or 0) >= 3)
            text = (f"🅰️ {name} set up {assists} goals against {opp}"
                    + (f" - the only player with {assists}+ assists in a {lg} match this season."
                       if n == 1 else "."))
            facts.append(Fact("performance", lg, name, text, rarity=7 + (assists - 3) + (n == 1),
                              evidence={"assists": assists, "opponent": opp, "date": p["date"],
                                        "matches_with_3_assists_this_season": n},
                              fact_id=f"assists|{p['match_id']}|{p['player_id']}", team=p["team"],
                              team_id=p["team_id"], context=1.5))
        gp = p.get("goals_prevented")
        if gp is not None and gp >= 1.5:
            top = abs(gp - best_gp) < 1e-9
            text = (f"🧤 {name} kept out {gp:.1f} more goals than expected against {opp}: "
                    f"{p['saves'] or 0} saves, {p['goals_conceded'] or 0} conceded from "
                    f"{p['xgot_faced'] or 0:.1f} xGOT"
                    + (f" - the best goalkeeping display in {the(lg)} this season." if top else ".")
                    + f"\n\n{pick(PUNCH['keeper'], name + str(p['match_id']))}")
            facts.append(Fact("performance", lg, name, text, rarity=5.5 + min(gp, 4) + 1.5 * top,
                              evidence={"goals_prevented": round(gp, 2), "saves": p["saves"],
                                        "conceded": p["goals_conceded"], "xgot_faced": p["xgot_faced"],
                                        "opponent": opp, "date": p["date"], "season_best": top},
                              fact_id=f"keeper|{p['match_id']}|{p['player_id']}", team=p["team"],
                              team_id=p["team_id"], context=1.5))
    return facts


# ------------------------------------------------------------------ 10. leaders
LEADER_METRICS = {   # column: (phrase with {v}, format, minimum, emoji, keepers only)
    "key_passes": ("has created {v} chances", "int", 20, "🎨", False),
    "big_chances_created": ("has created {v} big chances", "int", 6, "🎨", False),
    "xa": ("has {v} expected assists (xA)", "1", 3.0, "🎨", False),
    "dribbles": ("has completed {v} dribbles", "int", 15, "🪄", False),
    "tackles": ("has made {v} tackles", "int", 20, "🛡️", False),
    "interceptions": ("has made {v} interceptions", "int", 15, "🛡️", False),
    "goals_prevented": ("has prevented {v} goals (xGOT faced minus goals conceded)", "1", 2.5, "🧤", True),
}
SET_PIECES = {"FromCorner", "SetPiece", "FreeKick", "ThrowInSetPiece"}     # penalties apart


def leader_gap(values):
    """(leader key, value, runner-up value) when the leader is clearly ahead, else None."""
    if len(values) < 2:
        return None
    ranked = sorted(values.items(), key=lambda kv: -kv[1])
    (k, v), (_k2, v2) = ranked[0], ranked[1]
    if v >= 1.25 * v2 and v - v2 >= (1.0 if isinstance(v, float) else 3):
        return k, v, v2
    return None


def gen_leaders(d, lg):
    facts = []
    if d.complete.get(lg):
        players = [s for s in d.ps if s["league"] == lg and not s["hole"]]
        for col, (phrase, kind_fmt, minimum, emoji, keepers) in LEADER_METRICS.items():
            pool = [s for s in players if col in s and (not keepers or "saves" in s)]
            if not pool or any(col in s["missing"] for s in players if not keepers):
                continue                              # the column is not loaded for every match
            vals = {s["player_id"]: s[col] for s in pool}
            if kind_fmt == "1":
                vals = {k: round(v, 1) for k, v in vals.items()}
            lead = leader_gap(vals)
            if not lead or lead[1] < minimum:
                continue
            s = next(x for x in pool if x["player_id"] == lead[0])
            ctx = d.context(lg, s["team_id"])
            if ctx == 1.0:
                continue                              # not playing now: keep it for another day
            gap = lead[1] - lead[2]
            who = "keeper" if keepers else "player"
            text = (f"{emoji} {s['player']} {phrase.format(v=fmt(lead[1], kind_fmt))} in {the(lg)} this "
                    f"season - {fmt(gap, kind_fmt)} more than any other {who}.")
            if keepers:
                text += f"\n\n{pick(PUNCH['keeper'], s['player'])}"
            facts.append(Fact("leader", lg, s["player"], text, rarity=5 + min(lead[1] / lead[2] - 1, 1) * 4,
                              evidence={"stat": col, "value": fmt(lead[1], kind_fmt),
                                        "next_best": fmt(lead[2], kind_fmt), "games": s["games"]},
                              fact_id=f"leader|{lg}|{col}|{s['player_id']}|{fmt(lead[1], kind_fmt)}",
                              team=s["team"], team_id=s["team_id"], context=ctx))

    # teams: big chances missed, set-piece goals
    stats, names = team_season_stats(d, lg)
    if not season_under_way(stats):
        return facts
    if all("big_chances_missed" in s for s in stats.values()):
        vals = {t: int(s["big_chances_missed"]) for t, s in stats.items()}
        lead = leader_gap(vals)
        if lead and lead[1] >= 10:
            tid, v, v2 = lead
            text = (f"😬 {names[tid]} have missed {v} big chances in {the(lg)} this season - "
                    f"{v - v2} more than any other team.\n\n{pick(PUNCH['wasteful'], names[tid] + 'bcm')}")
            facts.append(Fact("leader", lg, names[tid], text, rarity=5 + min(v / v2 - 1, 1) * 4,
                              evidence={"stat": "big_chances_missed", "value": v, "next_best": v2},
                              fact_id=f"leader|{lg}|big_chances_missed|{tid}|{v}", team=names[tid],
                              team_id=tid, context=d.context(lg, tid)))
    detailed = {tid for tid in stats if all(r["detailed"] for r in d.team_seq[(lg, tid)]
                                            if r["season"] == d.season) and not d.hole(tid)}
    if len(detailed) == len(stats):
        set_goals, all_goals = defaultdict(int), defaultdict(int)
        for x in d.season_shots():
            if x["league"] == lg and x["result"] == "Goal" and not x["own_goal"]:
                all_goals[x["team_id"]] += 1
                set_goals[x["team_id"]] += x["situation"] in SET_PIECES
        lead = leader_gap({t: set_goals.get(t, 0) for t in stats})
        if lead and lead[1] >= 6:
            tid, v, v2 = lead
            text = (f"🎯 {names[tid]} have scored {v} of their {all_goals[tid]} {lg} goals from set pieces "
                    f"(corners, free kicks, throw-ins; no penalties) - the most in the league, "
                    f"{v - v2} more than anyone else.")
            facts.append(Fact("leader", lg, names[tid], text, rarity=5 + min(v / max(v2, 1) - 1, 1) * 4,
                              evidence={"stat": "set_piece_goals", "value": v, "next_best": v2,
                                        "all_goals_from_shots": all_goals[tid]},
                              fact_id=f"leader|{lg}|set_piece_goals|{tid}|{v}", team=names[tid],
                              team_id=tid, context=d.context(lg, tid)))
    return facts


GENERATORS = [gen_contrast, gen_streaks, gen_start, gen_overtake, gen_luck, gen_preview,
              gen_player_streaks, gen_pace, gen_performances, gen_leaders]


# ------------------------------------------------------------------ scoring and output
def finish_text(d, fact):
    tag = d.hashtag(fact.team) if fact.team else ""
    return f"{fact.text} {tag}".rstrip() if tag else fact.text


def find_facts(d):
    scorers = [p["player_id"] for p in d.pm
               if (p["goals"] or 0) > 0 and p["match_id"] in d.recent_matches.get(p["league"], set())]
    d.load_history(scorers)
    facts = []
    for lg in d.leagues:
        for gen in GENERATORS:
            facts += gen(d, lg)
    for f in facts:
        f.score = round(f.rarity * f.context * (1 + 0.25 * (d.popularity(f.team) - 1)), 2)
        f.final = finish_text(d, f)
        f.length = tweet_length(f.final)
    return facts


def draft(wh, today, leagues=None, top=25, include_repeats=False, md_path=None, log=print):
    """Find facts, keep the best `top`, store them in tweets.drafts. Returns the kept facts."""
    d = Data(wh, leagues, today)
    facts = find_facts(d)
    stamp = today.isoformat()
    cutoff = (today - timedelta(days=REPEAT_DAYS)).isoformat()
    seen = {r["fact_id"] for r in wh.read("tweets.drafts", ["fact_id", "run_date"],
                                          {"run_date": (">=", cutoff)}) if r["run_date"] < stamp}
    fresh = [f for f in facts if include_repeats or f.fact_id not in seen]
    too_long = [f for f in fresh if f.length > TWEET_LIMIT]
    fresh = sorted((f for f in fresh if f.length <= TWEET_LIMIT), key=lambda f: (-f.score, f.fact_id))
    chosen, per_entity, per_kind = [], defaultdict(int), defaultdict(int)
    kind_cap = max(1, math.ceil(top * MAX_SHARE_PER_KIND))
    for f in fresh:                          # at most 2 drafts about the same team/player
        if per_entity[f.entity] >= 2 or per_kind[f.kind] >= kind_cap:
            continue
        per_entity[f.entity] += 1
        per_kind[f.kind] += 1
        chosen.append(f)
        if len(chosen) >= top:
            break

    created = datetime.now(timezone.utc).strftime(DT)
    wh.replace("tweets.drafts", [{
        "run_date": stamp, "rank": i, "score": f.score, "type": f.kind, "league": f.league,
        "entity": f.entity, "team": f.team, "text": f.final, "length": f.length,
        "evidence": json.dumps(f.evidence, ensure_ascii=False), "fact_id": f.fact_id,
        "status": "draft", "created_at": created, "posted_at": None, "tweet_id": None}
        for i, f in enumerate(chosen, 1)], {"run_date": stamp})

    ctx = []
    for lg in d.leagues:
        rec = sorted({r["date"] for r in d.tm if r["match_id"] in d.recent_matches[lg]})
        ctx.append(f"- **{lg}**: last matchday {rec[0] + ' to ' + rec[-1] if rec else 'none'}; "
                   f"{len(d.upcoming[lg])} fixtures in the next {UPCOMING_DAYS} days")
    if md_path:
        with open(md_path, "w", encoding="utf-8") as out:
            out.write(f"# Tweet drafts - {stamp}\n\nSeason {season_label(d.season)}. "
                      f"{len(facts)} facts found, {len(fresh)} new, {len(chosen)} kept.\n\n"
                      + "\n".join(ctx) + "\n\nCheck every number against the evidence before posting.\n")
            for i, f in enumerate(chosen, 1):
                out.write(f"\n---\n\n### {i}. {f.kind} · {f.league} · {f.entity} · score {f.score} "
                          f"· {f.length}/{TWEET_LIMIT}\n\n")
                out.write("```\n" + f.final + "\n```\n\n")
                out.write("Evidence: " + " · ".join(f"{k} = {v}" for k, v in f.evidence.items()) + "\n")

    by_kind = defaultdict(int)
    for f in facts:
        by_kind[f.kind] += 1
    log(f"Season {season_label(d.season)}, leagues: {', '.join(d.leagues)}")
    log("\n".join(c.replace("**", "") for c in ctx))
    log(f"Facts found: {len(facts)} (" + ", ".join(f"{k} {v}" for k, v in sorted(by_kind.items()))
        + f"), {len(fresh)} not drafted in the last {REPEAT_DAYS} days, {len(too_long)} over "
        f"{TWEET_LIMIT} characters dropped, {len(chosen)} kept.")
    if not d.history_complete:
        log(f"Player history facts (overtake, pace) are off: {d.history_missing} played matches since "
            f"{season_label(min(d.first_season.values()))} still lack their details.")
    off = [lg for lg in d.leagues if not d.complete.get(lg)]
    if off:
        log(f"Player season facts (finishing, runs, leaders) are off for {', '.join(off)}: some matches of this "
            f"season lack their details.")
    for i, f in enumerate(chosen[:5], 1):
        log(f"\n{i}. [{f.kind} {f.score}] " + f.final.replace("\n\n", " "))
    return chosen

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
       luck       goals vs xG, points vs expected points, finishing (npg vs npxG)
       preview    head-to-head runs for the next fixtures
  3. Scoring: rarity x context (last matchday / next fixture) x club popularity, minus
     facts already drafted in the last 14 days.
  4. Templates write the tweet. Every number comes from the data; the evidence is stored
     next to each draft.

Reads the warehouse (core.*), writes tweets.drafts. The drafts table is also the history
that prevents repeats. History starts with the first season loaded for each league, so
facts say "since 2014/15", never "ever".
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
TWEET_LIMIT = 280
DT = "%Y-%m-%d %H:%M:%S"

PUNCH = {
    "bad": ["Nowhere near good enough. 🤷‍♂️", "Wasteful. 😬", "Something has to change. 👀",
            "Not the numbers you want. 📉"],
    "good": ["Clinical. 🎯", "Flying. 🚀", "Impressive. 👏", "Statement. 💪"],
    "lucky": ["Living on the edge. 😅", "Riding their luck? 🍀", "Can it last? 🤔"],
    "unlucky": ["Deserved more. 😤", "Football can be cruel. 💔", "The numbers say better times are coming. 📈"],
    "fire": ["On fire 🔥", "Unstoppable 🔥", "What a run 🔥"],
    # attack-specific and defence-specific lines, so "Wasteful" never lands on a defensive stat
    "bad_attack": ["Wasteful. 😬", "Where are the goals? 🤷‍♂️", "Not the numbers you want. 📉"],
    "bad_defence": ["Leaking. 😬", "Something has to change. 👀", "Not the numbers you want. 📉"],
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
           "team", "goals", "assists", "shots", "penalty_goals"]


class Data:
    def __init__(self, wh, leagues, today):
        self.wh, self.today = wh, today
        cutoff = today.isoformat()
        tm = [r for r in wh.read("core.team_matches")
              if r["match_id"] is not None and (r["date"] or "9") <= cutoff]
        tm.sort(key=lambda r: (r["kickoff_utc"] or "", r["match_id"]))
        if not tm:
            raise SystemExit("No team match data in the warehouse. Run the update first.")
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
        for r in tm:
            self.first_season[r["league"]] = min(self.first_season.get(r["league"], 9999), r["season"])
            self.team_seq[(r["league"], r["team_id"])].append(r)
            self.team_all[r["team_id"]].append(r)

        self.pm = [p for p in wh.read("core.player_matches", PM_COLS, {"season": self.season})
                   if (p["date"] or "9") <= cutoff]
        self.pm.sort(key=lambda p: (p["kickoff_utc"] or "", p["match_id"]))
        self.ps = wh.read("core.player_seasons", None, {"season": self.season})
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
        self._history = None
        self.unreliable = 0                          # scorers whose history didn't add up

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
        """Every season and every loaded match of these players (for the overtake facts)."""
        ids = sorted(set(player_ids))
        seasons, matches = defaultdict(list), defaultdict(list)
        if ids:
            for s in self.wh.read("core.player_seasons", None, {"player_id": ids}):
                seasons[s["player_id"]].append(s)
            for p in self.wh.read("core.player_matches", PM_COLS, {"player_id": ids}):
                if (p["date"] or "9") <= self.today.isoformat():
                    matches[(p["player_id"], p["league"], p["season"])].append(p)
        self._history = (seasons, matches)

    def club_tallies(self, pid):
        """{club: goals, assists, pens, games, first_season, league} over the loaded history,
        or None when a season can't be split between clubs reliably.

        A season at one club uses Understat's season totals. The current season and seasons
        with a mid-season move need match-level rows; they are only trusted when they add up
        to the season totals (otherwise some match details are missing)."""
        seasons, matches = self._history
        clubs = {}

        def add(team, league, season, goals, assists, pens, games):
            c = clubs.setdefault(team, {"goals": 0, "assists": 0, "pens": 0, "games": 0,
                                        "first_season": season, "league": league})
            c["goals"] += goals
            c["assists"] += assists
            c["pens"] += pens
            c["games"] += games
            if season < c["first_season"]:
                c["first_season"], c["league"] = season, league

        for s in seasons.get(pid, []):
            if s["season"] > self.season:
                continue
            split = s["teams"] and "," in s["teams"]
            if s["season"] < self.season and not split:
                add(s["teams"], s["league"], s["season"], s["goals"] or 0, s["assists"] or 0,
                    s["penalty_goals"] or 0, s["games"] or 0)
                continue
            rows = matches.get((pid, s["league"], s["season"]), [])
            if (sum(r["goals"] or 0 for r in rows) != (s["goals"] or 0)
                    or len(rows) != (s["games"] or 0)):
                return None
            for r in rows:
                add(r["team"], s["league"], s["season"], r["goals"] or 0, r["assists"] or 0,
                    r["penalty_goals"] or 0, 1)
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
CONTRAST_PAIRS = [("shots", "conversion", "bad", "good"), ("xg", "goals", "unlucky", "lucky"),
                  ("xga", "conceded", "unlucky", "lucky"), ("xpts", "points", "unlucky", "lucky")]


def team_season_stats(d, lg):
    stats = defaultdict(lambda: defaultdict(float))
    names = {}
    for (league, tid), rows in d.team_seq.items():
        if league != lg:
            continue
        for r in rows:
            if r["season"] != d.season:
                continue
            s = stats[tid]
            names[tid] = r["team"]
            s["games"] += 1
            s["goals"] += r["goals_for"] or 0
            s["conceded"] += r["goals_against"] or 0
            s["xg"] += r["xg"] or 0
            s["xga"] += r["xga"] or 0
            s["points"] += r["points"] or 0
            s["xpts"] += r["xpts"] or 0
    shots, player_goals, detailed = defaultdict(float), defaultdict(float), defaultdict(set)
    for p in d.pm:
        if p["league"] == lg:
            shots[p["team_id"]] += p["shots"] or 0
            player_goals[p["team_id"]] += p["goals"] or 0
            detailed[p["team_id"]].add(p["match_id"])
    for tid, s in stats.items():
        # shots only when every match of the team has its details: no half-season shot counts
        if shots.get(tid) and len(detailed[tid]) == s["games"]:
            s["shots"] = shots[tid]
            s["conversion"] = 100 * player_goals[tid] / shots[tid]
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
RUNS = {   # run: (test, words, minimum length, mood)
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
            if length > own_best:
                notes.append(f"their longest since {since}")
                rarity += 2
            if length > league_best:
                notes.append(f"the longest by any {lg} team since {since}")
                rarity += 3
            if not notes and length < minimum + 3:
                continue
            extra = f" - {' and '.join(notes)}" if notes else ""
            text = (f"📊 {team} are on a {length}-match {words} run in {the(lg)}{extra}."
                    f"\n\n{pick(PUNCH[mood], team + run)}")
            facts.append(Fact(
                "streak", lg, team, text, rarity=rarity,
                evidence={"run": run, "length": length, "team_best_before": own_best,
                          "other_teams_best": league_best, "last_match": last["date"]},
                fact_id=f"run|{lg}|{run}|{tid}|{length}", team=team, team_id=tid, context=1.5))
    return facts


# ------------------------------------------------------------------ 3. season start
START_METRICS = {   # key: (column, higher is better, words, punch line when it is the worst)
    "points": ("points", True, "points", "bad"),
    "goals": ("goals_for", True, "goals", "bad_attack"),
    "conceded": ("goals_against", False, "goals conceded", "bad_defence"),
    "xg": ("xg", True, "xG", "bad_attack"),
}


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
        for key, (col, hib, words, worst_mood) in START_METRICS.items():
            kind_fmt = "1" if key == "xg" else "int"
            total = lambda rs: sum((r[col] or 0) for r in rs[:n])
            now = total(cur)
            past = {s: total(rs) for (t, s), rs in by_team_season.items()
                    if t == tid and s != d.season and len(rs) >= n}
            if len(past) < 2:
                continue
            better = (lambda a, b: a > b) if hib else (lambda a, b: a < b)
            if all(better(now, v) for v in past.values()):
                kind = "best"
            elif all(better(v, now) for v in past.values()):
                kind = "worst"
            else:
                continue
            league_all = {(t, s): total(rs) for (t, s), rs in by_team_season.items()
                          if s != d.season and len(rs) >= n}
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
            mood = "good" if kind == "best" else worst_mood
            text += f"\n\n{pick(PUNCH[mood], team + key)}"
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
            d.unreliable += 1                        # history can't be split by club reliably
            continue
        here = clubs[team]
        before = here["goals"] - (p["goals"] or 0)
        for prev_team, c in clubs.items():
            if prev_team == team or c["goals"] < 5:
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
        for metric, real, exp in (("goals", "goals", "xg"), ("points", "points", "xpts")):
            diffs = {t: s[real] - s[exp] for t, s in stats.items()}
            for tid, sign in ((max(diffs, key=diffs.get), 1), (min(diffs, key=diffs.get), -1)):
                dv, s, team = diffs[tid], stats[tid], names[tid]
                if abs(dv) < 3 or dv * sign <= 0:
                    continue
                if metric == "goals":
                    text = (f"{'📈' if sign > 0 else '📉'} {team} have scored {fmt(s['goals'], 'int')} {lg} "
                            f"goals from {fmt(s['xg'], '1')} xG ({dv:+.1f}) - the biggest "
                            f"{'over' if sign > 0 else 'under'}performance in the league this season.")
                else:
                    text = (f"{'🍀' if sign > 0 else '💔'} {team} have {fmt(s['points'], 'int')} points but "
                            f"{fmt(s['xpts'], '1')} expected points ({dv:+.1f}) - the "
                            f"{'luckiest' if sign > 0 else 'unluckiest'} side in {the(lg)} on xPts.")
                text += f"\n\n{pick(PUNCH['lucky' if sign > 0 else 'unlucky'], team + metric)}"
                facts.append(Fact(
                    "luck", lg, team, text, rarity=4 + min(abs(dv), 10) / 2,
                    evidence={real: fmt(s[real], "int"), exp: fmt(s[exp], "1"), "difference": f"{dv:+.1f}",
                              "games": int(s["games"])},
                    fact_id=f"luck|{lg}|{metric}|{'over' if sign > 0 else 'under'}|{tid}",
                    team=team, team_id=tid, context=d.context(lg, tid)))
    # players: finishing (non-penalty goals vs npxG), at least 15 shots
    players = [s for s in d.ps if s["league"] == lg and (s["shots"] or 0) >= 15 and s["npxg"] is not None]
    if len(players) >= 10:
        diffs = {i: (s["non_penalty_goals"] or 0) - (s["npxg"] or 0) for i, s in enumerate(players)}
        top = max(diffs, key=diffs.get)
        s = players[top]
        if diffs[top] >= 2.5:
            team = (s["teams"] or "").split(",")[-1]
            tid = next((t for t, name in names.items() if name == team), None)
            text = (f"🎯 {s['player']} has {s['non_penalty_goals']} non-penalty {lg} goals from "
                    f"{s['npxg']:.1f} npxG ({diffs[top]:+.1f}) - the best finishing in the league this "
                    f"season.\n\n{pick(PUNCH['good'], s['player'])}")
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


GENERATORS = [gen_contrast, gen_streaks, gen_start, gen_overtake, gen_luck, gen_preview]


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
    chosen, per_entity = [], defaultdict(int)
    for f in fresh:                          # at most 2 drafts about the same team/player
        if per_entity[f.entity] >= 2:
            continue
        per_entity[f.entity] += 1
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
    if d.unreliable:
        log(f"Overtake facts: {d.unreliable} recent scorers left out because their match rows "
            f"don't add up to their season totals (missing match details).")
    for i, f in enumerate(chosen[:5], 1):
        log(f"\n{i}. [{f.kind} {f.score}] " + f.final.replace("\n\n", " "))
    return chosen

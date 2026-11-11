#!/usr/bin/env python3
"""
8_tweets.py - find interesting, matchday-related stats and draft tweets about them.

  python3 scripts/8_tweets.py                          # Premier League, top 15 drafts
  python3 scripts/8_tweets.py --leagues all --top 25
  python3 scripts/8_tweets.py --leagues "Premier League,La Liga"
  python3 scripts/8_tweets.py --date 2026-10-20        # pretend today is another day

How it works
  1. Context: the last matchday (matches played in the 4 days up to the latest result) and
     the fixtures of the next 7 days. Facts are about the teams and players in them.
  2. Fact patterns, each run over every team / player / stat (no hand-written facts):
       contrast   1st-3rd for one stat but bottom 5 for its partner (shots vs conversion...)
       streak     first clean sheet / win / away win in N matches; unbeaten, winning,
                  winless runs - compared with the team's and the league's longest since 2021/22
       start      points, goals, conceded, xG after N matches vs the same point of past seasons
       overtake   a scorer passes or matches his league tally for a previous club
       luck       goals vs xG, points vs expected points, finishing (npg vs npxG)
       preview    head-to-head runs for the next fixtures
  3. Scoring: rarity x context (last matchday / next fixture) x club popularity, minus
     facts already proposed in the last 14 days (output/tweets/history.csv).
  4. Templates write the tweet. Every number comes from the data; the evidence is printed
     under each draft so you can check it before posting.

Output: output/tweets/<date>.md (drafts + evidence) and output/tweets/<date>.csv
Optional: config/clubs.csv (club,hashtag,popularity) for hashtags and popularity weights.

Data: data/football_us.db (Understat, built by 4_build_sources.py). History starts in
2021/22, so facts say "since 2021/22", never "ever".
No third-party packages needed (Python 3.8+).
"""

import argparse
import csv
import hashlib
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

import paths

OUT_DIR = os.path.join(paths.OUTPUT_DIR, "tweets")
HISTORY = os.path.join(OUT_DIR, "history.csv")
CLUBS_FILE = os.path.join(paths.CONFIG_DIR, "clubs.csv")
FIRST_SEASON_LABEL = "2021/22"
RECENT_DAYS = 4          # a "matchday" = results within this many days of the latest one
UPCOMING_DAYS = 7
REPEAT_DAYS = 14         # don't propose a fact again if an earlier run proposed it in this window

PUNCH = {
    "bad": ["Nowhere near good enough. 🤷‍♂️", "Wasteful. 😬", "Something has to change. 👀",
            "Not the numbers you want. 📉"],
    "good": ["Clinical. 🎯", "Flying. 🚀", "Impressive. 👏", "Statement. 💪"],
    "lucky": ["Living on the edge. 😅", "Riding their luck? 🍀", "Can it last? 🤔"],
    "unlucky": ["Deserved more. 😤", "Football can be cruel. 💔", "The numbers say better times are coming. 📈"],
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


def ranks(values, higher_better):
    """Competition ranking (1 = best) with tie counts: {key: (rank, tied)}."""
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
    """'the 2nd-most shots', 'the joint-worst conversion rate', 'the most intense press'."""
    if rank <= math.ceil(n / 2):
        k, word = rank, best_word
    else:
        k, word = n - rank + 1, worst_word
    prefix = ("joint-" if tied else "") + ("" if k == 1 else f"{ordinal(k)}-")
    return f"the {prefix}{word} {noun}"


def season_label(s):
    return f"{s}/{str(s + 1)[2:]}"


def first_name(name):
    return (name or "").split()[0] if name else name


# ------------------------------------------------------------------ data
class Data:
    def __init__(self, leagues_arg, today):
        if not os.path.exists(paths.US_DB):
            sys.exit("data/football_us.db not found. Run scripts/4_build_sources.py first.")
        con = sqlite3.connect(paths.US_DB)
        con.row_factory = sqlite3.Row
        self.tm = [dict(r) for r in con.execute(
            "SELECT t.*, m.kickoff_utc FROM team_matches t JOIN matches m USING (match_id) "
            "WHERE t.match_id IS NOT NULL ORDER BY m.kickoff_utc, t.match_id")]
        self.pm = [dict(r) for r in con.execute(
            "SELECT p.*, m.kickoff_utc FROM player_matches p JOIN matches m USING (match_id) "
            "ORDER BY m.kickoff_utc")]
        self.ps = [dict(r) for r in con.execute("SELECT * FROM player_seasons")]
        self.fixtures = [dict(r) for r in con.execute(
            "SELECT * FROM fixtures WHERE status = 'upcoming' ORDER BY kickoff_utc")]
        con.close()
        if not self.tm:
            sys.exit("No team match data in football_us.db.")
        self.season = max(r["season"] for r in self.tm)
        all_leagues = sorted({r["league"] for r in self.tm})
        if leagues_arg.strip().lower() == "all":
            self.leagues = all_leagues
        else:
            self.leagues = [l.strip() for l in leagues_arg.split(",") if l.strip()]
            bad = [l for l in self.leagues if l not in all_leagues]
            if bad:
                sys.exit(f"Unknown league(s): {', '.join(bad)}. Available: {', '.join(all_leagues)}")
        self.today = today

        # context per league: last matchday's matches, next fixtures
        self.recent_matches, self.recent_teams, self.upcoming = {}, {}, {}
        for lg in self.leagues:
            played = [r for r in self.tm if r["league"] == lg and r["season"] == self.season
                      and r["date"] <= today.isoformat()]
            if not played:
                self.recent_matches[lg], self.recent_teams[lg] = set(), set()
            else:
                last = max(r["date"] for r in played)
                start = (date.fromisoformat(last) - timedelta(days=RECENT_DAYS - 1)).isoformat()
                rec = [r for r in played if r["date"] >= start]
                self.recent_matches[lg] = {r["match_id"] for r in rec}
                self.recent_teams[lg] = {r["team_id"] for r in rec}
            end = (today + timedelta(days=UPCOMING_DAYS)).isoformat()
            self.upcoming[lg] = [f for f in self.fixtures if f["league"] == lg
                                 and today.isoformat() <= f["date"] <= end]

        self.clubs = {}
        if os.path.exists(CLUBS_FILE):
            with open(CLUBS_FILE, encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("club"):
                        self.clubs[r["club"].strip()] = {
                            "hashtag": (r.get("hashtag") or "").strip(),
                            "popularity": float(r.get("popularity") or 1)}

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


class Fact:
    def __init__(self, kind, league, entity, text, rarity, evidence, fact_id, team=None,
                 team_id=None, context=1.0, hashtag_team=None):
        self.kind, self.league, self.entity, self.text = kind, league, entity, text
        self.rarity, self.evidence, self.fact_id = rarity, evidence, fact_id
        self.team, self.team_id, self.context = team, team_id, context
        self.hashtag_team = hashtag_team or team
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
    rows = [r for r in d.tm if r["league"] == lg and r["season"] == d.season]
    stats = defaultdict(lambda: defaultdict(float))
    names = {}
    for r in rows:
        s = stats[r["team_id"]]
        names[r["team_id"]] = r["team"]
        s["games"] += 1
        s["goals"] += r["goals_for"] or 0
        s["conceded"] += r["goals_against"] or 0
        s["xg"] += r["xg"] or 0
        s["xga"] += r["xga"] or 0
        s["points"] += r["points"] or 0
        s["xpts"] += r["xpts"] or 0
    shots = defaultdict(float)
    player_goals = defaultdict(float)
    for p in d.pm:
        if p["league"] == lg and p["season"] == d.season:
            shots[p["team_id"]] += p["shots"] or 0
            player_goals[p["team_id"]] += p["goals"] or 0
    for tid, s in stats.items():
        if shots.get(tid):
            s["shots"] = shots[tid]
            s["conversion"] = 100 * player_goals[tid] / shots[tid]
    return stats, names


def gen_contrast(d, lg):
    stats, names = team_season_stats(d, lg)
    n = len(stats)
    if n < 10 or sorted(s["games"] for s in stats.values())[n // 2] < 5:
        return []                                          # too early in the season
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
            text = (f"{team} have {pa} in the {lg} ({fmt(va, ma[4])}) "
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
    seq = defaultdict(list)
    for r in d.tm:
        if r["league"] == lg:
            seq[r["team_id"]].append(r)
    return seq


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
                text = (f"🚨 {team} have {words} in the {lg} for the first time in {gap} {match_word}{months}."
                        f"\n\nThe last time was against {prev_row['opponent']} on {prev_row['date']}.")
            else:
                text = (f"🚨 {team} have {words} in the {lg} for the first time in our data "
                        f"(since {FIRST_SEASON_LABEL}).")
            facts.append(Fact(
                "streak", lg, team, text, rarity=5 + min(gap, 30) / 4,
                evidence={"event": ev, "matches_without": gap, "ended_vs": opp, "date": last["date"],
                          "previous": ev_rows[prev[-1]]["date"] if prev else "none since 2021/22",
                          "counted": match_word},
                fact_id=f"drought|{lg}|{ev}|{tid}|{last['match_id']}", team=team, team_id=tid,
                context=1.5))
        # ongoing runs
        for run, (test, words, minimum, mood) in RUNS.items():
            length = run_length(rows, test)
            if length < minimum:
                continue
            own_best = longest_run(rows[:-length], test) if len(rows) > length else 0
            league_best = max((longest_run(r2, test) for t2, r2 in seq.items() if t2 != tid), default=0)
            notes, rarity = [], 3 + length / 3
            if length > own_best:
                notes.append(f"their longest since {FIRST_SEASON_LABEL}")
                rarity += 2
            if length > league_best:
                notes.append(f"the longest by any {lg} team since {FIRST_SEASON_LABEL}")
                rarity += 3
            if not notes and length < minimum + 3:
                continue
            extra = f" - {' and '.join(notes)}" if notes else ""
            text = (f"📊 {team} are on a {length}-match {words} run in the {lg}{extra}."
                    f"\n\n{pick(PUNCH[mood], team + run)}")
            facts.append(Fact(
                "streak", lg, team, text, rarity=rarity,
                evidence={"run": run, "length": length, "team_best_before": own_best,
                          "other_teams_best": league_best, "last_match": last["date"]},
                fact_id=f"run|{lg}|{run}|{tid}|{length}", team=team, team_id=tid, context=1.5))
    return facts


# ------------------------------------------------------------------ 3. season start
START_METRICS = {   # key: (column, higher is better, words)
    "points": ("points", True, "points"),
    "goals": ("goals_for", True, "goals"),
    "conceded": ("goals_against", False, "goals conceded"),
    "xg": ("xg", True, "xG"),
}


def gen_start(d, lg):
    seq = team_sequences(d, lg)
    facts = []
    by_team_season = defaultdict(list)
    for tid, rows in seq.items():
        for r in rows:
            by_team_season[(tid, r["season"])].append(r)
    for tid, rows in seq.items():
        cur = by_team_season.get((tid, d.season), [])
        n = len(cur)
        if n < 4 or cur[-1]["match_id"] not in d.recent_matches.get(lg, set()):
            continue
        team = cur[-1]["team"]
        best_fact = None
        for key, (col, hib, words) in START_METRICS.items():
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
            league_record = league_all and all(better(now, v) for v in league_all.values())
            ref_s, ref_v = (max if (kind == "best") == hib else min)(past.items(), key=lambda kv: kv[1])
            v_txt = fmt(now, "1" if key == "xg" else "int")
            ref_txt = fmt(ref_v, "1" if key == "xg" else "int")
            if league_record and kind == "best":
                text = (f"📈 {team} have {v_txt} {words} after {n} {lg} matches - the best start by "
                        f"any {lg} team since {FIRST_SEASON_LABEL}.")
                rarity = 9
            else:
                adj = "best" if kind == "best" else "worst"
                k = len(past) + 1
                text = (f"📊 {team} have {v_txt} {words} after {n} {lg} matches - their {adj} start in "
                        f"any of their {k} {lg} seasons since {FIRST_SEASON_LABEL} (previous {adj}: "
                        f"{ref_txt} in {season_label(ref_s)}).")
                rarity = 6 if kind == "best" else 6.5
            mood = "good" if kind == "best" else "bad"
            text += f"\n\n{pick(PUNCH[mood], team + key)}"
            f = Fact("start", lg, team, text, rarity=rarity,
                     evidence={"stat": key, "now": v_txt, "matches": n, "kind": kind,
                               "past_seasons": "; ".join(f"{season_label(s)}: {fmt(v, '1' if key == 'xg' else 'int')}"
                                                         for s, v in sorted(past.items())),
                               "league_record": bool(league_record)},
                     fact_id=f"start|{lg}|{key}|{kind}|{tid}|{n}", team=team, team_id=tid, context=1.5)
            if best_fact is None or f.rarity > best_fact.rarity:
                best_fact = f
        if best_fact:
            facts.append(best_fact)
    return facts


# ------------------------------------------------------------------ 4. overtake
def gen_overtake(d, lg):
    facts = []
    recent = [p for p in d.pm if p["league"] == lg and p["season"] == d.season
              and p["match_id"] in d.recent_matches.get(lg, set()) and (p["goals"] or 0) > 0]
    opp_of = {(r["match_id"], r["team_id"]): r["opponent"] for r in d.tm}
    for p in recent:
        pid, team = p["player_id"], p["team"]
        # this season at the current club (league matches downloaded so far)
        cur_rows = [q for q in d.pm if q["player_id"] == pid and q["season"] == d.season
                    and q["team_id"] == p["team_id"]]
        cur = {"goals": sum(q["goals"] or 0 for q in cur_rows),
               "assists": sum(q["assists"] or 0 for q in cur_rows),
               "pens": sum(q["penalty_goals"] or 0 for q in cur_rows), "games": len(cur_rows)}
        # past seasons, single-club seasons only (a mid-season move can't be split)
        clubs = defaultdict(lambda: {"goals": 0, "assists": 0, "pens": 0, "games": 0, "leagues": set()})
        for s in d.ps:
            if s["player_id"] == pid and s["season"] < d.season and s["teams"] and "," not in s["teams"]:
                c = clubs[s["teams"]]
                c["goals"] += s["goals"] or 0
                c["assists"] += s["assists"] or 0
                c["pens"] += s["penalty_goals"] or 0
                c["games"] += s["games"] or 0
                c["leagues"].add(s["league"])
        here = clubs.pop(team, {"goals": 0, "assists": 0, "pens": 0, "games": 0})
        total = {k: here[k] + cur[k] for k in ("goals", "assists", "pens", "games")}
        before = total["goals"] - (p["goals"] or 0)
        for prev_team, c in clubs.items():
            if c["goals"] < 5:
                continue
            if before < c["goals"] < total["goals"]:
                verb = "overtook"
            elif before < c["goals"] == total["goals"]:
                verb = "matched"
            else:
                continue
            opp = opp_of.get((p["match_id"], p["team_id"]), "")
            goal_word = "goal" if (p["goals"] or 0) == 1 else "goals"
            prev_lg = "/".join(sorted(c["leagues"]))
            text = (f"⚽️ {p['player']}'s {goal_word} against {opp} meant he {verb} his total league "
                    f"tally for {prev_team} ({c['goals']}).")
            if total["pens"] == 0 and c["pens"] > 0:
                text += (f"\n\n{first_name(p['player'])} has yet to score a penalty for {team} in the "
                         f"league, while {c['pens']} of his {c['goals']} for {prev_team} came from the spot.")
            ga = total["goals"] + total["assists"]
            text += f"\n\n{ga} G/A in {total['games']} games for {team}"
            text += f"… {pick(PUNCH['fire'], p['player'])}" if total["games"] and ga / total["games"] >= 0.5 else "."
            facts.append(Fact(
                "overtake", lg, p["player"], text, rarity=7 + min(c["goals"], 40) / 10,
                evidence={"goals_for_club": total["goals"], "before_match": before,
                          f"goals_for_{prev_team}": c["goals"], "previous_league": prev_lg,
                          "pens_now": total["pens"], "pens_before": c["pens"],
                          "games_for_club": total["games"], "assists_for_club": total["assists"],
                          "note": "seasons from 2021/22; mid-season moves left out"},
                fact_id=f"overtake|{pid}|{prev_team}|{verb}", team=team, team_id=p["team_id"],
                context=1.5))
    return facts


# ------------------------------------------------------------------ 5. luck
def gen_luck(d, lg):
    stats, names = team_season_stats(d, lg)
    n = len(stats)
    facts = []
    if n >= 10 and sorted(s["games"] for s in stats.values())[n // 2] >= 5:
        for metric, real, exp, words in (("goals", "goals", "xg", "xG"), ("points", "points", "xpts", "expected points")):
            diffs = {t: s[real] - s[exp] for t, s in stats.items()}
            for tid, sign in ((max(diffs, key=diffs.get), 1), (min(diffs, key=diffs.get), -1)):
                dv, s, team = diffs[tid], stats[tid], names[tid]
                if abs(dv) < 3:
                    continue
                if metric == "goals":
                    text = (f"{'📈' if sign > 0 else '📉'} {team} have scored {fmt(s['goals'], 'int')} {lg} "
                            f"goals from {fmt(s['xg'], '1')} xG ({dv:+.1f}) - the biggest "
                            f"{'over' if sign > 0 else 'under'}performance in the league this season.")
                else:
                    text = (f"{'🍀' if sign > 0 else '💔'} {team} have {fmt(s['points'], 'int')} points but "
                            f"{fmt(s['xpts'], '1')} expected points ({dv:+.1f}) - the "
                            f"{'luckiest' if sign > 0 else 'unluckiest'} side in the {lg} on xPts.")
                text += f"\n\n{pick(PUNCH['lucky' if sign > 0 else 'unlucky'], team + metric)}"
                facts.append(Fact(
                    "luck", lg, team, text, rarity=4 + min(abs(dv), 10) / 2,
                    evidence={real: fmt(s[real], "int"), exp: fmt(s[exp], "1"), "difference": f"{dv:+.1f}",
                              "games": int(s["games"])},
                    fact_id=f"luck|{lg}|{metric}|{'over' if sign > 0 else 'under'}|{tid}",
                    team=team, team_id=tid, context=d.context(lg, tid)))
    # players: finishing (non-penalty goals vs npxG), at least 15 shots
    players = [s for s in d.ps if s["league"] == lg and s["season"] == d.season
               and (s["shots"] or 0) >= 15 and s["npxg"] is not None]
    if len(players) >= 10:
        diffs = {i: (s["non_penalty_goals"] or 0) - (s["npxg"] or 0) for i, s in enumerate(players)}
        top = max(diffs, key=diffs.get)
        s = players[top]
        if diffs[top] >= 2.5:
            team = s["teams"].split(",")[-1]
            text = (f"🎯 {s['player']} has {s['non_penalty_goals']} non-penalty {lg} goals from "
                    f"{s['npxg']:.1f} npxG ({diffs[top]:+.1f}) - the best finishing in the league this "
                    f"season.\n\n{pick(PUNCH['good'], s['player'])}")
            tid = next((r["team_id"] for r in d.tm if r["team"] == team and r["league"] == lg), None)
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
        meetings = [r for r in d.tm if r["team_id"] == h and r["opponent_id"] == a]
        if len(meetings) < 4:
            continue
        day = datetime.strptime(f["kickoff_utc"], "%Y-%m-%d %H:%M:%S").strftime("%A")
        for team, opp, rows, tid in ((f["home_team"], f["away_team"], meetings, h),
                                     (f["away_team"], f["home_team"],
                                      [r for r in d.tm if r["team_id"] == a and r["opponent_id"] == h], a)):
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
            facts.append(Fact(
                "preview", lg, f"{team} v {opp}", text, rarity=rarity,
                evidence={"run": kind, "meetings_considered": len(rows), "last_meeting": last["date"],
                          "last_score": f"{last['goals_for']}-{last['goals_against']}",
                          "fixture": f"{f['home_team']} v {f['away_team']} {f['kickoff_utc']} UTC"},
                fact_id=f"preview|{f['match_id']}|{tid}|{kind}", team=team, team_id=tid, context=1.3))
    return facts


def best_per_fixture(facts, d):
    """Both sides of a fixture tell the same story: keep the stronger one (popularity breaks ties)."""
    best = {}
    weight = lambda f: f.rarity * (1 + 0.25 * (d.popularity(f.team) - 1))
    for f in facts:
        key = f.fact_id.split("|")[1]
        if key not in best or weight(f) > weight(best[key]):
            best[key] = f
    return list(best.values())


GENERATORS = [gen_contrast, gen_streaks, gen_start, gen_overtake, gen_luck,
              lambda d, lg: best_per_fixture(gen_preview(d, lg), d)]


# ------------------------------------------------------------------ scoring and output
def load_history():
    if not os.path.exists(HISTORY):
        return {}
    seen = {}
    with open(HISTORY, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            seen[r["fact_id"]] = max(seen.get(r["fact_id"], ""), r["date"])
    return seen


def finish_text(d, fact):
    tag = d.hashtag(fact.hashtag_team) if fact.hashtag_team else ""
    return f"{fact.text} {tag}".rstrip() if tag else fact.text


def main():
    ap = argparse.ArgumentParser(description="Draft matchday-related stat tweets.")
    ap.add_argument("--leagues", default="Premier League", help='comma-separated, or "all"')
    ap.add_argument("--top", type=int, default=15, help="number of drafts (default 15)")
    ap.add_argument("--date", help="today's date, YYYY-MM-DD (default: today)")
    ap.add_argument("--include-repeats", action="store_true",
                    help=f"also show facts proposed in the last {REPEAT_DAYS} days")
    args = ap.parse_args()
    today = date.fromisoformat(args.date) if args.date else date.today()
    d = Data(args.leagues, today)

    facts = []
    for lg in d.leagues:
        for gen in GENERATORS:
            facts += gen(d, lg)
    seen = load_history()
    cutoff = (today - timedelta(days=REPEAT_DAYS)).isoformat()
    fresh = []
    for f in facts:
        f.score = round(f.rarity * f.context * (1 + 0.25 * (d.popularity(f.team) - 1)), 2)
        if not args.include_repeats and cutoff <= seen.get(f.fact_id, "") < today.isoformat():
            continue
        fresh.append(f)
    fresh.sort(key=lambda f: -f.score)
    chosen, per_entity = [], defaultdict(int)
    for f in fresh:                         # at most 2 drafts about the same team/player
        if per_entity[f.entity] >= 2:
            continue
        per_entity[f.entity] += 1
        chosen.append(f)
        if len(chosen) >= args.top:
            break

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = today.isoformat()
    md_path, csv_path = os.path.join(OUT_DIR, f"{stamp}.md"), os.path.join(OUT_DIR, f"{stamp}.csv")
    ctx = []
    for lg in d.leagues:
        rec = sorted({r["date"] for r in d.tm if r["match_id"] in d.recent_matches[lg]})
        ctx.append(f"- **{lg}**: last matchday {rec[0] + ' to ' + rec[-1] if rec else 'none'}; "
                   f"{len(d.upcoming[lg])} fixtures in the next {UPCOMING_DAYS} days")
    with open(md_path, "w", encoding="utf-8") as out:
        out.write(f"# Tweet drafts - {stamp}\n\nSeason {season_label(d.season)}. "
                  f"{len(facts)} facts found, {len(fresh)} new, {len(chosen)} kept.\n\n"
                  + "\n".join(ctx) + "\n\nCheck every number against the evidence before posting.\n")
        for i, f in enumerate(chosen, 1):
            text = finish_text(d, f)
            warn = f"  ⚠️ {len(text)} characters (over 280)" if len(text) > 280 else ""
            out.write(f"\n---\n\n### {i}. {f.kind} · {f.entity} · score {f.score}{warn}\n\n")
            out.write("```\n" + text + "\n```\n\n")
            out.write("Evidence: " + " · ".join(f"{k} = {v}" for k, v in f.evidence.items()) + "\n")
    with open(csv_path, "w", encoding="utf-8", newline="") as out:
        w = csv.writer(out)
        w.writerow(["rank", "score", "type", "league", "entity", "tweet", "evidence", "fact_id"])
        for i, f in enumerate(chosen, 1):
            w.writerow([i, f.score, f.kind, f.league, f.entity, finish_text(d, f),
                        "; ".join(f"{k}={v}" for k, v in f.evidence.items()), f.fact_id])
    new_file = not os.path.exists(HISTORY)
    with open(HISTORY, "a", encoding="utf-8", newline="") as out:
        w = csv.writer(out)
        if new_file:
            w.writerow(["date", "fact_id", "type", "entity"])
        already_today = {k for k, v in seen.items() if v == stamp}
        for f in chosen:
            if f.fact_id not in already_today:
                w.writerow([stamp, f.fact_id, f.kind, f.entity])

    by_kind = defaultdict(int)
    for f in facts:
        by_kind[f.kind] += 1
    print(f"Season {season_label(d.season)}, leagues: {', '.join(d.leagues)}")
    print("\n".join(c.replace("**", "") for c in ctx))
    print(f"Facts found: {len(facts)} (" + ", ".join(f"{k} {v}" for k, v in sorted(by_kind.items()))
          + f"), {len(fresh)} not proposed in the last {REPEAT_DAYS} days, {len(chosen)} kept.")
    for i, f in enumerate(chosen[:5], 1):
        print(f"\n{i}. [{f.kind} {f.score}] " + finish_text(d, f).replace("\n\n", " "))
    print(f"\nAll drafts: {md_path}")


if __name__ == "__main__":
    main()
